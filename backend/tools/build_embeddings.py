import argparse
import json
import logging
import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.search_engine import SearchEngine


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Build and persist paper embeddings for the configured paper source.'
    )
    parser.add_argument(
        '--db-path',
        default=None,
        help='Optional local sqlite path or SQLAlchemy database URL.',
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='Rebuild all embeddings even when text_hash has not changed.',
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=16,
        help='Embedding API batch size. Default: 16.',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Report pending embeddings without writing to SQLite or calling the API.',
    )
    parser.add_argument(
        '--verbose',
        action='store_true',
        help='Enable verbose logging.',
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format='%(levelname)s %(name)s: %(message)s',
    )

    engine = SearchEngine(db_path=args.db_path)
    result = engine.build_paper_embeddings(
        force=args.force,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
    )
    result['db_path'] = engine.db_display
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
