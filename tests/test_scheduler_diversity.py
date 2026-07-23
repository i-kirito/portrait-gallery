import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from scheduler import DailyScheduler  # noqa: E402


class ScheduleDiversityTest(unittest.TestCase):
    def make_scheduler(self, data_dir: str) -> DailyScheduler:
        return DailyScheduler({"config": {"timezone": "Asia/Shanghai"}}, data_dir)

    def test_all_schedule_prompt_variants_require_safe_adult_daily_photos(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = self.make_scheduler(tmpdir)
            today = date(2026, 7, 18)
            prompts = (
                scheduler._build_schedule_prompt(today, "（无）", "（无）", ""),
                scheduler._build_compact_schedule_prompt(today, "（无）", "（无）", ""),
                scheduler._build_emergency_schedule_prompt(today, "（无）", "（无）", ""),
            )

        for prompt in prompts:
            self.assertIn("明确 25 岁以上成年女性", prompt)
            self.assertIn("聊天人设中的年龄", prompt)
            self.assertIn("服装必须完整、不透视", prompt)

    def test_all_schedule_prompt_variants_require_solo_character_focus(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = self.make_scheduler(tmpdir)
            today = date(2026, 7, 23)
            prompts = (
                scheduler._build_schedule_prompt(today, "（无）", "（无）", ""),
                scheduler._build_compact_schedule_prompt(today, "（无）", "（无）", ""),
                scheduler._build_emergency_schedule_prompt(today, "（无）", "（无）", ""),
            )

        for prompt in prompts:
            self.assertIn("生图镜头原则", prompt)
            self.assertIn("交给你自行判断", prompt)
            self.assertIn("镜头里只能清楚拍到角色本人", prompt)
            self.assertNotIn("hand in hand", prompt)
            self.assertNotIn("schedule_details.action_en", prompt)

    def test_allows_bed_idle_opening_and_multiple_cooking(self):
        """Bed-idle first item and multi cooking are no longer hard post-check limits."""
        scheduler = self.make_scheduler("data")
        items = scheduler._schedule_plan_items(
            "08:23 赖床窝在被子里翻手机看消息\n"
            "12:38 在厨房为自己做午餐\n"
            "15:20 去书店挑选新的小说\n"
            "19:24 准备晚餐并收拾餐桌"
        )

        self.assertEqual("", scheduler._schedule_diversity_error(items))

    def test_accepts_varied_schedule(self):
        scheduler = self.make_scheduler("data")
        items = scheduler._schedule_plan_items(
            "08:23 去楼下取一杯热拿铁\n"
            "10:17 整理书桌和今日灵感板\n"
            "12:38 在咖啡馆吃轻食午餐\n"
            "15:20 去书店挑选新的小说\n"
            "19:24 沿着河边散步听播客\n"
            "22:18 做睡前护肤准备休息"
        )

        self.assertEqual("", scheduler._schedule_diversity_error(items))

    def test_post_check_rejects_recent_similar_actions_as_second_guard(self):
        """Dual guard layer 2: obvious same-action paraphrases must fail post-check."""
        with tempfile.TemporaryDirectory() as tmpdir:
            schedule_data = {
                "2026-07-21": {
                    "status": "ok",
                    "schedule": (
                        "08:15 出门前往体育公园，在沿途的早餐铺买一份全麦三明治\n"
                        "10:42 在公园跑道上完成五公里慢跑训练，中途在树荫下喝水休息\n"
                        "15:53 在厨房用牛油果和鸡胸肉做了一份高蛋白轻食碗"
                    ),
                },
            }
            Path(tmpdir, "schedule_data.json").write_text(
                json.dumps(schedule_data, ensure_ascii=False),
                encoding="utf-8",
            )
            scheduler = self.make_scheduler(tmpdir)
            recent_actions = scheduler._recent_schedule_actions(date(2026, 7, 22))
            items = scheduler._schedule_plan_items(
                "08:20 前往体育公园路上买一份全麦三明治和冰美式\n"
                "10:50 在跑道完成五公里慢跑训练并在树荫下休息\n"
                "16:10 去书店挑选新的小说"
            )

            error = scheduler._schedule_diversity_error(
                items,
                recent_actions=recent_actions,
            )

            self.assertIn("相同或高度相似的日程动作", error)
            self.assertIn("2026-07-21", error)

    def test_schedule_history_exposes_full_actions_for_llm_judgment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            schedule_data = {
                "2026-07-21": {
                    "status": "ok",
                    "schedule": (
                        "08:24 起床后在书桌前整理今天的学习计划\n"
                        "10:15 去楼下便利店挑选几款新出的气泡水\n"
                        "14:45 在书房里用平板电脑绘制新的插画草图"
                    ),
                },
            }
            Path(tmpdir, "schedule_data.json").write_text(
                json.dumps(schedule_data, ensure_ascii=False),
                encoding="utf-8",
            )
            scheduler = self.make_scheduler(tmpdir)
            history = scheduler._get_schedule_history(date(2026, 7, 22))

            self.assertIn("[2026-07-21]", history)
            self.assertIn("08:24 起床后在书桌前整理今天的学习计划", history)
            self.assertIn("14:45 在书房里用平板电脑绘制新的插画草图", history)
            # Full action text, not the old truncated multi-activity one-liner summary.
            self.assertNotIn(" / ", history)

    def test_accepts_different_actions_after_recent_sports_day(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            schedule_data = {
                "2026-07-21": {
                    "status": "ok",
                    "schedule": (
                        "08:15 出门前往体育公园买早餐\n"
                        "10:42 在公园跑道完成五公里慢跑训练\n"
                        "21:14 在瑜伽垫上做一组睡前拉伸"
                    ),
                },
            }
            Path(tmpdir, "schedule_data.json").write_text(
                json.dumps(schedule_data, ensure_ascii=False),
                encoding="utf-8",
            )
            scheduler = self.make_scheduler(tmpdir)
            recent_actions = scheduler._recent_schedule_actions(date(2026, 7, 22))
            items = scheduler._schedule_plan_items(
                "08:23 去楼下取一杯热拿铁\n"
                "10:17 整理书桌和今日灵感板\n"
                "12:38 在咖啡馆吃轻食午餐\n"
                "15:20 去书店挑选新的小说\n"
                "19:24 沿着河边散步听播客\n"
                "22:18 做睡前护肤准备休息"
            )

            self.assertEqual(
                "",
                scheduler._schedule_diversity_error(
                    items,
                    recent_actions=recent_actions,
                ),
            )

    def test_prompt_requires_llm_to_judge_three_day_action_dedupe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            schedule_data = {
                "2026-07-21": {
                    "status": "ok",
                    "schedule": "08:24 去楼下便利店买气泡水\n10:15 在书房绘制插画草图",
                },
            }
            Path(tmpdir, "schedule_data.json").write_text(
                json.dumps(schedule_data, ensure_ascii=False),
                encoding="utf-8",
            )
            scheduler = self.make_scheduler(tmpdir)
            history = scheduler._get_schedule_history(date(2026, 7, 22))
            prompt = scheduler._build_schedule_prompt(
                date(2026, 7, 22),
                "（无）",
                history,
                "",
            )

            self.assertIn("近 3 天完整日程动作", prompt)
            self.assertIn("双保障第 1 层", prompt)
            self.assertIn("去楼下便利店买气泡水", prompt)
            self.assertIn("同义改写、换说法、换时间点、换地点词仍算重复", prompt)
            self.assertIn("生成 schedule 时先执行双保障第 1 层", prompt)

    def test_history_keeps_accessories_beyond_the_old_sixty_character_cutoff(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            schedule_data = {
                "2026-07-05": {
                    "status": "ok",
                    "outfit_style": "清新风",
                    "outfit": (
                        "风格：清新风\n"
                        "发型：灰粉色长发扎成高马尾，额前空气刘海轻盈，两侧留出微卷碎发\n"
                        "穿搭：浅蓝色衬衫搭配白色阔腿裤和帆布鞋，"
                        "颈间佩戴一条银色十字星锁骨链"
                    ),
                },
                "2026-07-02": {
                    "status": "ok",
                    "outfit_style": "复古风",
                    "outfit": "风格：复古风\n穿搭：这条记录在三天窗口之外",
                },
            }
            Path(tmpdir, "schedule_data.json").write_text(
                json.dumps(schedule_data, ensure_ascii=False),
                encoding="utf-8",
            )
            scheduler = self.make_scheduler(tmpdir)

            history = scheduler._get_history(date(2026, 7, 6))

            self.assertIn("银色十字星锁骨链", history)
            self.assertIn("银色星形项链", history)
            self.assertNotIn("2026-07-02", history)

    def test_rejects_recent_accessory_with_synonymous_wording(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            schedule_data = {
                "2026-07-05": {
                    "status": "ok",
                    "outfit": (
                        "风格：清新风\n"
                        "穿搭：浅蓝色衬衫搭配白色阔腿裤，"
                        "颈间佩戴一条银色十字星锁骨链"
                    ),
                },
            }
            Path(tmpdir, "schedule_data.json").write_text(
                json.dumps(schedule_data, ensure_ascii=False),
                encoding="utf-8",
            )
            scheduler = self.make_scheduler(tmpdir)
            recent = scheduler._recent_outfit_accessories(date(2026, 7, 6))
            candidate = {
                "outfit": "风格：甜美风\n穿搭：粉色连衣裙，搭配银色星形吊坠项链",
            }

            error = scheduler._outfit_accessory_repeat_error(candidate, recent)

            self.assertIn("银色星形项链", error)
            self.assertIn("2026-07-05", error)

    def test_accepts_a_different_recent_accessory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            schedule_data = {
                "2026-07-05": {
                    "status": "ok",
                    "outfit": "风格：清新风\n穿搭：白色连衣裙，佩戴银色十字星锁骨链",
                },
            }
            Path(tmpdir, "schedule_data.json").write_text(
                json.dumps(schedule_data, ensure_ascii=False),
                encoding="utf-8",
            )
            scheduler = self.make_scheduler(tmpdir)
            recent = scheduler._recent_outfit_accessories(date(2026, 7, 6))
            candidate = {
                "outfit": "风格：复古风\n穿搭：酒红色衬衫，搭配珍珠耳钉",
            }

            self.assertEqual(
                "",
                scheduler._outfit_accessory_repeat_error(candidate, recent),
            )

    def test_reads_accessories_from_schedule_detail_outfit_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            schedule_data = {
                "2026-07-05": {
                    "status": "ok",
                    "outfit": "风格：清新风\n穿搭：白色衬衫搭配蓝色长裙",
                    "schedule_details": [{
                        "outfit_en": (
                            "white blouse, blue midi skirt, white shoes, "
                            "silver cross star necklace"
                        ),
                    }],
                },
            }
            Path(tmpdir, "schedule_data.json").write_text(
                json.dumps(schedule_data, ensure_ascii=False),
                encoding="utf-8",
            )
            scheduler = self.make_scheduler(tmpdir)
            recent = scheduler._recent_outfit_accessories(date(2026, 7, 6))
            candidate = {
                "outfit": "风格：甜美风\n穿搭：粉色连衣裙",
                "schedule_details": [{
                    "outfit_en": "pink dress, white shoes, silver star pendant necklace",
                }],
            }

            error = scheduler._outfit_accessory_repeat_error(candidate, recent)

            self.assertIn("银色星形项链", error)

    def test_rejects_disliked_outfit_with_synonymous_wording(self):
        scheduler = self.make_scheduler("data")
        disliked = {
            "date": "2026-07-14",
            "outfit": {
                "发型": "高颅顶半扎发",
                "穿搭": (
                    "白色高领针织打底搭配黑色西装马甲和深灰直筒西装裤，"
                    "脚穿黑色乐福鞋，配银色几何耳环"
                ),
            },
        }
        candidate = {
            "outfit_style": "冷御风",
            "outfit": (
                "风格：冷御风\n发型：利落半扎长发\n"
                "穿搭：纯白turtleneck knit top叠穿black tailored waistcoat，"
                "配charcoal straight-leg trousers和black loafer shoes"
            ),
        }

        error = scheduler._disliked_outfit_similarity_error(candidate, [disliked])

        self.assertIn("高度相似", error)
        self.assertIn("西装马甲", error)

    def test_rejects_common_synonyms_for_current_disliked_outfit(self):
        scheduler = self.make_scheduler("data")
        disliked = {
            "date": "2026-07-14",
            "outfit": {
                "发型": "灰粉色长发自然披散",
                "穿搭": (
                    "奶杏色V领针织短袖上衣，搭配深棕色高腰阔腿裤、"
                    "棕色皮质凉拖、金色圆形耳环和棕色皮质手链"
                ),
            },
            "outfit_keywords": (
                "cream beige V-neck knit top, dark brown high-waist wide-leg trousers, "
                "brown leather slide sandals"
            ),
        }
        candidate = {
            "outfit_style": "日常风",
            "outfit": (
                "风格：日常风\n发型：低马尾\n"
                "穿搭：奶油杏色罗纹V领短袖衫，搭配深咖啡色高腰曳地喇叭裤，"
                "脚穿棕色皮质穆勒鞋"
            ),
            "schedule_details": [{
                "outfit_en": (
                    "cream ribbed tee, dark brown high-rise palazzo pants, "
                    "brown leather mules"
                ),
                "hair_en": "low ponytail",
            }],
        }

        error = scheduler._disliked_outfit_similarity_error(candidate, [disliked])

        self.assertIn("高度相似", error)
        self.assertIn("长裤", error)
        self.assertIn("凉鞋/凉拖", error)

    def test_accepts_same_style_with_substantially_different_garments(self):
        scheduler = self.make_scheduler("data")
        disliked = {
            "date": "2026-07-14",
            "outfit_style": "冷御风",
            "outfit": {
                "发型": "利落半扎发",
                "穿搭": (
                    "白色高领针织上衣叠穿黑色西装马甲，搭配深灰直筒裤和黑色乐福鞋"
                ),
            },
        }
        candidate = {
            "outfit_style": "冷御风",
            "outfit": (
                "风格：冷御风\n发型：侧编发\n"
                "穿搭：酒红色雪纺A字连衣裙，搭配金色玛丽珍鞋和珍珠耳钉"
            ),
        }

        self.assertEqual(
            "",
            scheduler._disliked_outfit_similarity_error(candidate, [disliked]),
        )

    def test_rejects_same_garment_combination_under_a_different_style(self):
        scheduler = self.make_scheduler("data")
        disliked = {
            "date": "2026-07-14",
            "outfit_style": "冷御风",
            "outfit": {
                "穿搭": (
                    "白色高领无袖针织上衣，黑色西装马甲，深灰高腰阔腿裤，黑色短靴"
                ),
            },
        }
        candidate = {
            "outfit_style": "复古风",
            "outfit": (
                "风格：复古风\n发型：低马尾\n"
                "穿搭：纯白高领无袖毛衣外搭黑色正装马甲，"
                "下穿炭灰高腰宽腿长裤，搭配黑色ankle boots"
            ),
        }

        error = scheduler._disliked_outfit_similarity_error(candidate, [disliked])

        self.assertIn("高度相似", error)

    def test_disliked_similarity_reads_schedule_detail_outfit_en(self):
        scheduler = self.make_scheduler("data")
        disliked = {
            "date": "2026-07-14",
            "outfit": {
                "穿搭": (
                    "白色高领针织上衣搭配黑色西装马甲、深灰直筒裤和黑色乐福鞋"
                ),
            },
        }
        candidate = {
            "outfit": "风格：清新风\n发型：双马尾\n穿搭：一套蓝色日常服装",
            "schedule_details": [{
                "outfit_en": (
                    "white high-neck knit top, black tailored vest, "
                    "charcoal straight-leg trousers, black loafers"
                ),
                "hair_en": "twin ponytails",
            }],
        }

        error = scheduler._disliked_outfit_similarity_error(candidate, [disliked])

        self.assertIn("高度相似", error)

    def test_emergency_prompt_keeps_disliked_outfit_constraint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            disliked_data = {
                "items": [{
                    "date": "2026-07-14",
                    "outfit_style": "复古风",
                    "outfit": {
                        "风格": "复古风",
                        "穿搭": "奶杏色V领针织短袖搭配深棕高腰阔腿裤和棕色凉拖",
                    },
                }],
            }
            Path(tmpdir, "disliked_outfits.json").write_text(
                json.dumps(disliked_data, ensure_ascii=False),
                encoding="utf-8",
            )
            scheduler = self.make_scheduler(tmpdir)

            prompt = scheduler._build_emergency_schedule_prompt(date(2026, 7, 15))

            self.assertIn("禁止复现的不喜欢穿搭", prompt)
            self.assertIn("奶杏色V领针织短袖", prompt)
            self.assertIn("只改风格名或同义说法仍算重复", prompt)

    def test_current_cold_dislikes_calibrate_as_similar(self):
        scheduler = self.make_scheduler("data")
        recent = {
            "date": "2026-07-14",
            "outfit": {
                "发型": "高颅顶半扎发",
                "穿搭": (
                    "白色修身半高领无袖针织打底衫外搭黑色西装马甲，"
                    "深炭灰高腰直筒西装九分裤配黑色漆皮方头乐福鞋"
                ),
            },
            "outfit_keywords": (
                "white high-neck knit top, black tailored vest, "
                "dark charcoal straight-leg trousers, black patent leather loafers"
            ),
        }
        older = {
            "date": "2026-06-16",
            "outfit": {
                "发型": "中分直发",
                "穿搭": (
                    "黑色修身西装马甲内搭纯白高领无袖针织衫，"
                    "高腰深灰阔腿西装裤配黑色尖头短靴"
                ),
            },
            "outfit_keywords": (
                "suit vest, high-neck knit top, wide-leg trousers, ankle boots"
            ),
        }

        similar, score, _shared = scheduler._is_disliked_outfit_similar(recent, older)

        self.assertTrue(similar)
        self.assertGreaterEqual(score, 0.64)


if __name__ == "__main__":
    unittest.main()
