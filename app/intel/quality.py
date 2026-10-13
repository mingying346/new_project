"""质量清单收集器：把海量问题条目压缩成"分类聚合 + 有限样本"。

设计理由：全量流水约 300 万行，若逐格登记"字段为空"会产生数十万条
噪声，办案人员无法复核。因此：
  * 行级问题（重复、列数不齐、金额/时间不可解析、方向非法、不可见字符）
    逐条保留（含来源文件与原始行号），每类每文件最多 MAX_EXAMPLES 条；
  * 空值按 数据集+字段 聚合计数，并附首批样本位置；
  * 所有原始行仍完整入库，清单只做导航，不做数据裁剪。
"""
from __future__ import annotations

from collections import Counter, defaultdict

MAX_EXAMPLES = 200

CATEGORIES = ["缺失", "重复", "异常", "解析失败"]


class Collector:
    def __init__(self):
        self.rows: list[tuple] = []                 # 行级条目
        self.gap: Counter = Counter()                # (dataset, field) -> 空值次数
        self.gap_sample: dict = defaultdict(list)    # (dataset, field) -> [(path,line)]
        self.per_file_row_issue: Counter = Counter()  # (path, category) -> 已写入样本数
        self.dropped: Counter = Counter()            # (dataset, category, kind) -> 压缩计数
        self.total_row_issues = 0

    def issue(self, category, severity, dataset, path, line, field, value, detail, action):
        key = (path, category)
        if category in ("重复", "异常"):
            self.per_file_row_issue[key] += 1
            if self.per_file_row_issue[key] > MAX_EXAMPLES:
                self.total_row_issues += 1
                self.dropped[(dataset, category, field)] += 1
                return
        self.rows.append((category, severity, dataset, path, line, field, value, detail, action))

    def flush_dropped(self):
        """被样本上限压缩掉的行级问题必须聚合登记，不得静默丢弃。"""
        for (dataset, category, field), n in sorted(self.dropped.items(), key=lambda kv: -kv[1]):
            self.rows.append((category, "中", dataset, f"聚合({n}处)", 0, field, f"{n}",
                             f"{dataset} 中字段「{field}」类问题共 {n} 处，超出单文件样本上限 "
                             f"{MAX_EXAMPLES} 条，未逐条展示；全量数据仍完整入库，可用 SQL 按类别检索",
                             "如需逐条清单，调大 MAX_EXAMPLES 后重跑 tidy"))

    def missing(self, dataset, path, line, field, detail="字段为空（源数据即为空，未做任何补齐）"):
        self.gap[(dataset, field)] += 1
        if len(self.gap_sample[(dataset, field)]) < 5:
            self.gap_sample[(dataset, field)].append((path, line))

    def flush_gaps(self):
        for (dataset, field), n in sorted(self.gap.items(), key=lambda kv: -kv[1]):
            samples = "; ".join(f"{p}:{l}" for p, l in self.gap_sample[(dataset, field)])
            self.rows.append(("缺失", "高" if n > 100000 else ("中" if n > 1000 else "低"), dataset,
                              f"聚合({n}处)", 0, field, f"{n}",
                              f"字段[{field}]在 {dataset} 中为空 {n} 次；未做任何补齐",
                              f"样本位置：{samples}"))

    def summary(self):
        c = Counter(r[0] for r in self.rows)
        c["未样本化的行级问题"] = self.total_row_issues
        return dict(c)
