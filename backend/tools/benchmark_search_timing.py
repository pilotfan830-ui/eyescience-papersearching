import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.search_engine import SearchEngine  # noqa: E402


DEFAULT_QUERIES = [
    '白内障最新治疗有哪些？',
    '帮我找近五年关于myopia control的论文',
    '王海燕的青光眼相关文章',
    '糖尿病视网膜病变的AI筛查研究',
    'retina OCT segmentation 的经典论文',
    '不要李伟作者的干眼症研究',
    '角膜移植术后并发症怎么检索相关论文？',
    'intraocular pressure 和 glaucoma progression 的关系',
    '赵明教授关于斜视治疗的文章',
    '儿童近视 prevention and outdoor activity 相关研究',
]

STEP_LABELS = {
    'detect_database': '定位数据库',
    'load_papers': '加载论文数据',
    'build_paper_lookup': '建立论文索引',
    'configure_ai_and_cache': '初始化 AI 与缓存配置',
    'build_fts_index': '构建 FTS 内存索引',
    'semantic_embedding_matrix_load': '加载论文向量矩阵',
    'local_embedding_model_load': '加载本地 embedding 模型',
    'qwen_client_config_check': '检查 Qwen 配置',
    'rank_cache_key': '生成缓存键',
    'rank_cache_lookup': '读取缓存',
    'parse_query': '解析查询条件',
    'query_rewrite': '改写查询',
    'query_rewrite_ai': 'AI 改写查询',
    'raw_fts': '原始查询 FTS 召回',
    'rewrite_fts': '改写查询 FTS 补召回',
    'embedding_recall': 'embedding 语义召回',
    'author_recall': '作者召回',
    'merge_candidates': '合并候选论文',
    'local_rank': '本地综合打分',
    'rerank': 'AI 重排',
    'build_recall_queries': '构造召回查询列表',
    'fts_multi_retrieve_total': '多路召回总耗时',
    'fts_retrieve': 'FTS 全文检索',
    'keyword_retrieve': '关键词字段检索',
    'author_retrieve': '作者字段检索',
    'lexical_fallback_retrieve': '词面兜底检索',
    'semantic_retrieve_total': '语义召回总耗时',
    'semantic_query_embedding_ai': 'AI 生成 query 向量',
    'semantic_similarity_dot': '向量相似度计算',
    'semantic_topn_extract': '提取语义 TopN',
    'author_retrieve_final': '最终作者补充召回',
    'must_term_fts_retrieve': 'must term FTS 检索',
    'must_term_keyword_retrieve': 'must term 关键词检索',
    'merge_candidate_ids': '合并候选论文',
    'score_candidates': '候选打分',
    'initial_sort': '初次排序',
    'qwen_rerank_ai': 'AI 重排结果',
    'blend_qwen_scores': '融合 AI 重排分数',
    'sort_results': '最终排序',
    'rank_cache_store': '写入缓存',
    'unknown': '未知步骤',
}


def _new_step_stats() -> Dict[str, float]:
    return {
        'total_ms': 0.0,
        'calls': 0,
        'searches': 0,
    }


def _collect_timing(stats: Dict[str, Dict], timing: Dict) -> None:
    seen = set()
    for step in timing.get('steps') or []:
        name = str(step.get('name') or 'unknown')
        duration_ms = float(step.get('duration_ms') or 0.0)
        stats[name]['total_ms'] += duration_ms
        stats[name]['calls'] += 1
        seen.add(name)
    for name in seen:
        stats[name]['searches'] += 1


def _label_step(name: str) -> str:
    return STEP_LABELS.get(name, name)


def _print_breakdown(title: str, timing: Dict) -> None:
    print(f'\n{title}')
    print('-' * 72)
    print(f'总耗时: {float(timing.get("total_ms") or 0.0):.3f} ms')
    for step in timing.get('steps') or []:
        name = str(step.get('name') or 'unknown')
        duration_ms = float(step.get('duration_ms') or 0.0)
        print(f'- {_label_step(name)} ({name}): {duration_ms:.3f} ms')


def _print_table(stats: Dict[str, Dict], runs: int) -> None:
    rows = []
    for name, data in stats.items():
        total_ms = float(data['total_ms'])
        calls = int(data['calls'])
        rows.append((
            name,
            calls,
            total_ms / max(calls, 1),
            total_ms / max(runs, 1),
            total_ms,
        ))
    rows.sort(key=lambda row: row[4], reverse=True)

    print('\n各步骤平均耗时')
    print('-' * 126)
    print(f'{"步骤":20} {"内部名":34} {"调用次数":>10} {"单次平均ms":>14} {"每次搜索平均ms":>18} {"总耗时ms":>14}')
    print('-' * 126)
    for name, calls, avg_call, avg_search, total_ms in rows:
        print(
            f'{_label_step(name)[:20]:20} {name[:34]:34} '
            f'{calls:10d} {avg_call:14.3f} {avg_search:18.3f} {total_ms:14.3f}'
        )


def run_benchmark(args: argparse.Namespace) -> Dict:
    engine = SearchEngine(db_path=args.db_path)
    if args.disable_ai:
        engine._rewrite_switch_on = False
        engine.rewrite_enabled = False
        engine._qwen_rerank_switch_on = False
        engine.qwen_rerank_enabled = False
        engine.embedding_api_key = ''

    warmup_timing = engine.warm_ai_components()
    queries: List[str] = args.query or DEFAULT_QUERIES
    stats = defaultdict(_new_step_stats)
    total_search_ms = 0.0
    result_counts = []
    cache_hits = 0

    started_at = time.perf_counter()
    for index in range(args.runs):
        query = queries[index % len(queries)]
        payload = engine.search_papers(
            query,
            topk=args.limit,
            sort=args.sort,
            include_timing=True,
            use_cache=False,
        )
        timing = payload.get('timing') or {}
        _collect_timing(stats, timing)
        total_search_ms += float(timing.get('total_ms') or 0.0)
        result_counts.append(len(payload.get('results') or []))
        if payload.get('cache_hit'):
            cache_hits += 1
        if not args.quiet and not args.json:
            print(
                f'[{index + 1}/{args.runs}] '
                f'查询="{query}" '
                f'本次耗时={float(timing.get("total_ms") or 0.0):.3f} ms '
                f'结果数={len(payload.get("results") or [])}',
                flush=True,
            )
    wall_ms = (time.perf_counter() - started_at) * 1000

    step_summary = {}
    for name, data in stats.items():
        total_ms = float(data['total_ms'])
        calls = int(data['calls'])
        step_summary[name] = {
            'calls': calls,
            'total_ms': round(total_ms, 3),
            'avg_per_call_ms': round(total_ms / max(calls, 1), 3),
            'avg_per_search_ms': round(total_ms / max(args.runs, 1), 3),
            'searches_seen': int(data['searches']),
        }

    startup_total_ms = (
        float(engine.startup_timing.get('total_ms') or 0.0)
        + float(warmup_timing.get('total_ms') or 0.0)
    )
    return {
        'runs': args.runs,
        'queries': queries,
        'limit': args.limit,
        'sort': args.sort,
        'cache_hits': cache_hits,
        'startup_timing': engine.startup_timing,
        'ai_warmup_timing': warmup_timing,
        'startup_and_ai_warmup_ms': round(startup_total_ms, 3),
        'search_total_ms': round(total_search_ms, 3),
        'search_avg_ms': round(total_search_ms / max(args.runs, 1), 3),
        'wall_ms': round(wall_ms, 3),
        'wall_avg_ms': round(wall_ms / max(args.runs, 1), 3),
        'overall_avg_with_startup_ms': round((startup_total_ms + total_search_ms) / max(args.runs, 1), 3),
        'avg_result_count': round(sum(result_counts) / max(len(result_counts), 1), 3),
        'steps': dict(sorted(step_summary.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description='重复模拟论文搜索并统计耗时。')
    parser.add_argument('--runs', type=int, default=10, help='模拟搜索次数。')
    parser.add_argument('--limit', type=int, default=20, help='每次搜索返回结果数。')
    parser.add_argument('--sort', default='best_match', choices=['best_match', 'most_recent'])
    parser.add_argument('--query', action='append', help='指定查询语句，可重复传入多次。')
    parser.add_argument('--db-path', default=None, help='可选：指定 sqlite 数据库路径。')
    parser.add_argument('--disable-ai', action='store_true', help='关闭外部 AI 调用，做离线基线测试。')
    parser.add_argument('--quiet', action='store_true', help='不打印每次搜索的进度。')
    parser.add_argument('--json', action='store_true', help='只输出 JSON。')
    args = parser.parse_args()

    result = run_benchmark(args)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print('搜索耗时基准测试')
    print(f'模拟次数: {result["runs"]}')
    print(f'查询条数: {len(result["queries"])}')
    print(f'缓存命中次数: {result["cache_hits"]}')
    print(f'启动 + AI 预热耗时: {result["startup_and_ai_warmup_ms"]:.3f} ms')
    print(f'平均每次搜索耗时: {result["search_avg_ms"]:.3f} ms')
    print(f'平均每次搜索耗时（含启动 + AI 预热）: {result["overall_avg_with_startup_ms"]:.3f} ms')
    print(f'平均结果数: {result["avg_result_count"]:.3f}')
    _print_breakdown('启动阶段时间分解', result['startup_timing'])
    _print_breakdown('AI 预热时间分解', result['ai_warmup_timing'])
    _print_table(result['steps'], result['runs'])


if __name__ == '__main__':
    main()
