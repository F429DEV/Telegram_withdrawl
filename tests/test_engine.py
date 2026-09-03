"""抽奖算法的测试：跑 python -m unittest discover -s tests"""

import unittest
from collections import Counter
from dataclasses import dataclass

from lottery import engine


@dataclass
class P:
    user_id: int
    weight: int = 1


class TestEngine(unittest.TestCase):
    def test_deterministic(self):
        people = [P(i) for i in range(50)]
        a = engine.draw(people, 3, "seed-abc", 7)
        b = engine.draw(people, 3, "seed-abc", 7)
        self.assertEqual([p.user_id for p in a], [p.user_id for p in b])

    def test_seed_changes_result(self):
        people = [P(i) for i in range(200)]
        a = [p.user_id for p in engine.draw(people, 5, "seed-1", 1)]
        b = [p.user_id for p in engine.draw(people, 5, "seed-2", 1)]
        self.assertNotEqual(a, b)

    def test_no_duplicates_and_count(self):
        people = [P(i) for i in range(20)]
        wins = engine.draw(people, 5, engine.new_seed(), 1)
        self.assertEqual(len(wins), 5)
        self.assertEqual(len({p.user_id for p in wins}), 5)

    def test_more_winners_than_people(self):
        people = [P(i) for i in range(3)]
        wins = engine.draw(people, 10, engine.new_seed(), 1)
        self.assertEqual(len(wins), 3)

    def test_empty(self):
        self.assertEqual(engine.draw([], 3, "s", 1), [])
        self.assertEqual(engine.draw([P(1)], 0, "s", 1), [])

    def test_uniform_enough(self):
        """10 个人抽 1 个，跑 20000 次，每人应该在 10% 附近。"""
        people = [P(i) for i in range(10)]
        counter = Counter()
        for n in range(20000):
            winner = engine.draw(people, 1, f"seed-{n}", 1)[0]
            counter[winner.user_id] += 1
        for uid in range(10):
            share = counter[uid] / 20000
            self.assertGreater(share, 0.085, f"用户 {uid} 中奖率偏低: {share}")
            self.assertLess(share, 0.115, f"用户 {uid} 中奖率偏高: {share}")

    def test_weight_helps(self):
        """权重 10 的人应该明显比权重 1 的人更容易中。"""
        people = [P(0, weight=10)] + [P(i, weight=1) for i in range(1, 10)]
        hits = 0
        for n in range(5000):
            if engine.draw(people, 1, f"w-{n}", 1, weighted=True)[0].user_id == 0:
                hits += 1
        share = hits / 5000
        self.assertGreater(share, 0.35, f"高权重用户中奖率只有 {share}")

    def test_weight_ignored_when_not_weighted(self):
        people = [P(0, weight=100)] + [P(i, weight=1) for i in range(1, 10)]
        hits = sum(
            1 for n in range(5000)
            if engine.draw(people, 1, f"u-{n}", 1, weighted=False)[0].user_id == 0
        )
        self.assertLess(hits / 5000, 0.15)

    def test_draw_names(self):
        names = ["张三", "李四", "王五", "张三"]
        wins, seed = engine.draw_names(names, 2)
        self.assertEqual(len(wins), 2)
        self.assertEqual(len(set(wins)), 2)
        again, _ = engine.draw_names(names, 2, seed)
        self.assertEqual(wins, again)


if __name__ == "__main__":
    unittest.main()
