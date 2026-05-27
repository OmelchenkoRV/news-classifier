"""
Consolidate Fragmented Triggers
==================================

One-time script to merge existing fragmented triggers based on shared
core anchors. Run this after applying the hierarchical merging fix
to clean up triggers created under the old logic.

What it does:
  1. Find all non-archived triggers in the same category
  2. Group them by shared core anchors (iran, fed, sec, etc.)
  3. Within each group, merge smaller triggers into the largest one
     (the one with most mentions and earliest first_seen_at)
  4. Reassign trigger_mentions to the surviving trigger
  5. Update classifications to point to the surviving trigger

Usage:
    python -m pipeline.consolidate_triggers --dry-run
    python -m pipeline.consolidate_triggers
"""

import os
import sys
import argparse
import logging
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.database import get_connection, get_cursor
from pipeline.triggers import CORE_ANCHORS, hybrid_similarity

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def find_merge_candidates(conn):
    """
    Find groups of triggers that should merge based on shared core anchors.
    
    Returns list of (survivor_id, [merged_ids]) tuples.
    """
    cur = get_cursor(conn)
    cur.execute("""
        SELECT id, signature, category, keywords, entities, actor_pairs,
               first_seen_at, mention_count, state
        FROM triggers
        WHERE state != 'ARCHIVED'
        ORDER BY category, mention_count DESC, first_seen_at ASC
    """)
    triggers = cur.fetchall()
    
    # Group by category
    by_category = defaultdict(list)
    for t in triggers:
        by_category[t['category']].append(t)
    
    merge_groups = []
    
    for category, cat_triggers in by_category.items():
        # Build groups based on shared core anchors
        # Each trigger may belong to multiple groups initially
        # We'll resolve overlaps by picking the largest group
        
        merged = set()  # IDs already in some merge group
        
        for primary in cat_triggers:
            if primary['id'] in merged:
                continue
            
            primary_anchors = set(primary['entities'] or [])
            primary_core = primary_anchors & CORE_ANCHORS
            
            if not primary_core:
                continue
            
            # Find all triggers sharing at least one core anchor with this one
            mergeables = []
            for other in cat_triggers:
                if other['id'] == primary['id'] or other['id'] in merged:
                    continue
                
                other_anchors = set(other['entities'] or [])
                other_core = other_anchors & CORE_ANCHORS
                shared_core = primary_core & other_core
                
                if not shared_core:
                    continue
                
                # Compute similarity to verify they're related
                sim = hybrid_similarity(
                    set(primary['keywords']),
                    primary_anchors,
                    set(primary['actor_pairs'] or []),
                    set(other['keywords']),
                    other_anchors,
                    set(other['actor_pairs'] or []),
                )
                
                # If they share a core anchor AND have any meaningful similarity, merge
                if sim >= 0.20:
                    mergeables.append((other['id'], sim, other['signature']))
            
            if mergeables:
                merge_groups.append((
                    primary['id'],
                    primary['signature'],
                    [m[0] for m in mergeables],
                    [m[2] for m in mergeables],
                ))
                merged.add(primary['id'])
                merged.update(m[0] for m in mergeables)
    
    return merge_groups


def execute_merge(conn, survivor_id, merged_ids, dry_run=False):
    """
    Merge multiple triggers into one survivor.
    
    Steps:
      1. Get all data from triggers being merged
      2. Update survivor with combined keywords/anchors/pairs/mentions
      3. Reassign trigger_mentions and classifications to survivor
      4. Delete merged triggers
    """
    cur = get_cursor(conn)
    
    # Fetch all triggers
    cur.execute("""
        SELECT id, keywords, entities, actor_pairs, mention_count, first_seen_at, last_seen_at
        FROM triggers WHERE id = ANY(%s)
    """, ([survivor_id] + merged_ids,))
    rows = cur.fetchall()
    
    if not rows:
        return 0
    
    # Combine all data
    all_keywords = set()
    all_anchors = set()
    all_pairs = set()
    total_mentions = 0
    earliest = rows[0]['first_seen_at']
    latest = rows[0]['last_seen_at']
    
    for r in rows:
        all_keywords.update(r['keywords'] or [])
        all_anchors.update(r['entities'] or [])
        all_pairs.update(r['actor_pairs'] or [])
        total_mentions += r['mention_count'] or 0
        if r['first_seen_at'] < earliest:
            earliest = r['first_seen_at']
        if r['last_seen_at'] > latest:
            latest = r['last_seen_at']
    
    if dry_run:
        return total_mentions
    
    # Update survivor with merged data
    cur.execute("""
        UPDATE triggers
        SET keywords = %s,
            entities = %s,
            actor_pairs = %s,
            mention_count = %s,
            first_seen_at = %s,
            last_seen_at = %s
        WHERE id = %s
    """, (list(all_keywords), list(all_anchors), list(all_pairs),
          total_mentions, earliest, latest, survivor_id))
    
    # Reassign trigger_mentions to survivor (handle conflicts)
    cur.execute("""
        UPDATE trigger_mentions
        SET trigger_id = %s
        WHERE trigger_id = ANY(%s)
          AND headline_id NOT IN (
              SELECT headline_id FROM trigger_mentions WHERE trigger_id = %s
          )
    """, (survivor_id, merged_ids, survivor_id))
    
    # Delete duplicate mentions
    cur.execute("""
        DELETE FROM trigger_mentions WHERE trigger_id = ANY(%s)
    """, (merged_ids,))
    
    # Reassign classifications
    cur.execute("""
        UPDATE classifications
        SET trigger_id = %s
        WHERE trigger_id = ANY(%s)
    """, (survivor_id, merged_ids))
    
    # Delete the merged triggers
    cur.execute("DELETE FROM triggers WHERE id = ANY(%s)", (merged_ids,))
    
    return total_mentions


def consolidate(dry_run=False):
    conn = get_connection()
    
    logger.info("Finding merge candidates...")
    groups = find_merge_candidates(conn)
    
    if not groups:
        logger.info("No fragmented triggers found. Nothing to merge.")
        conn.close()
        return
    
    logger.info(f"Found {len(groups)} merge groups:")
    print()
    
    total_merged = 0
    total_mentions_consolidated = 0
    
    for survivor_id, survivor_sig, merged_ids, merged_sigs in groups:
        print(f"  KEEP:  [{survivor_id}] {survivor_sig}")
        for mid, msig in zip(merged_ids, merged_sigs):
            print(f"   merge: [{mid}] {msig}")
        
        mentions = execute_merge(conn, survivor_id, merged_ids, dry_run=dry_run)
        total_merged += len(merged_ids)
        total_mentions_consolidated += mentions
        print()
    
    if not dry_run:
        conn.commit()
    
    conn.close()
    
    print(f"{'='*60}")
    print(f"Summary {'(DRY RUN)' if dry_run else ''}:")
    print(f"  Merge groups: {len(groups)}")
    print(f"  Triggers consolidated: {total_merged}")
    print(f"  Mentions reassigned: {total_mentions_consolidated}")
    
    if dry_run:
        print()
        print("Run without --dry-run to apply changes.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Consolidate fragmented triggers")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without applying them")
    args = parser.parse_args()
    consolidate(dry_run=args.dry_run)
