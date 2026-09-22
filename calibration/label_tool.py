"""Labeling tool for NLP calibration.

Interactive CLI to label events for supervised learning.
Usage: python label_tool.py [--count N] [--unlabeled-only]

Reads events from the database, presents them for labeling,
and stores results in training_examples table.
"""

import asyncio
import argparse
import sys
from datetime import datetime, timezone
from typing import Optional

import asyncpg

# Database connection (adjust as needed)
DB_DSN = "postgresql://survival:survival@localhost:5432/survival_map"

LAYER_OPTIONS = {
    '1': 'bus',
    '2': 'cops',
    '3': 'traffic',
    '4': 'pig',
    '5': 'other',
    'q': None,  # skip
}

async def get_unlabeled_events(conn, count: int) -> list:
    """Fetch events not yet in training_examples."""
    return await conn.fetch("""
        SELECT e.id, e.description, e.layer, e.strategy, 
               e.confidence, e.matches, e.event_time
        FROM events e
        WHERE e.id NOT IN (SELECT event_id FROM training_examples WHERE event_id IS NOT NULL)
          AND e.description IS NOT NULL
          AND e.description != 'без описания'
          AND e.strategy != 'random'
        ORDER BY e.event_time DESC
        LIMIT $1
    """, count)


async def insert_label(conn, event_id: int, text: str, 
                       detected_layer: str, detected_strategy: str,
                       detected_confidence: float, detected_geo_ids: list,
                       correct_layer: str, notes: str = None):
    """Insert a labeled example."""
    await conn.execute("""
        INSERT INTO training_examples 
        (event_id, text, detected_layer, detected_strategy, detected_confidence,
         detected_geo_ids, correct_layer, labeled_by, notes)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
    """, event_id, text, detected_layer, detected_strategy, detected_confidence,
         detected_geo_ids, correct_layer, 'cli_tool', notes)


def parse_geo_ids(matches) -> list:
    """Extract geo_ids from matches JSONB."""
    if not matches:
        return []
    if isinstance(matches, str):
        import json
        matches = json.loads(matches)
    return [m.get('geo_id') for m in matches if m.get('geo_id')]


async def label_events(count: int, unlabeled_only: bool):
    """Main labeling loop."""
    conn = await asyncpg.connect(DB_DSN)
    
    try:
        events = await get_unlabeled_events(conn, count)
        if not events:
            print("No unlabeled events found.")
            return
        
        print(f"\nLabeling {len(events)} events. Commands:")
        print("  1=bus, 2=cops, 3=traffic, 4=pig, 5=other, q=skip")
        print("  Type notes after layer (optional): '1 text about road'")
        print()
        
        labeled = 0
        for event in events:
            event_id = event['id']
            text = event['description'][:200]
            detected_layer = event['layer'] or 'unknown'
            detected_strategy = event['strategy'] or 'unknown'
            detected_confidence = event['confidence'] or 0.0
            geo_ids = parse_geo_ids(event['matches'])
            
            print(f"--- Event {event_id} ({event['event_time']:%Y-%m-%d %H:%M}) ---")
            print(f"Text: {text}")
            print(f"Detected: layer={detected_layer}, strategy={detected_strategy}, "
                  f"confidence={detected_confidence:.3f}, geo_ids={geo_ids}")
            
            try:
                user_input = input("Label: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting.")
                break
            
            if not user_input or user_input.lower() == 'q':
                continue
            
            parts = user_input.split(maxsplit=1)
            layer_code = parts[0]
            notes = parts[1] if len(parts) > 1 else None
            
            correct_layer = LAYER_OPTIONS.get(layer_code)
            if correct_layer is None and layer_code != 'q':
                print(f"Invalid option: {layer_code}. Skipping.")
                continue
            
            await insert_label(
                conn, event_id, event['description'],
                detected_layer, detected_strategy, detected_confidence,
                geo_ids, correct_layer, notes,
            )
            labeled += 1
            print(f"  -> Labeled as {correct_layer}")
        
        print(f"\nDone. Labeled {labeled}/{len(events)} events.")
        
    finally:
        await conn.close()


def main():
    parser = argparse.ArgumentParser(description='Label events for NLP calibration')
    parser.add_argument('--count', type=int, default=20, help='Number of events to label')
    parser.add_argument('--unlabeled-only', action='store_true', default=True,
                       help='Only show unlabeled events')
    args = parser.parse_args()
    
    asyncio.run(label_events(args.count, args.unlabeled_only))


if __name__ == '__main__':
    main()
