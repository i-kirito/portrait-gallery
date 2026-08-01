import asyncio
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


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

    def test_llm_sends_xiaohongshu_reference_as_multimodal_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = self.make_scheduler(tmpdir)
            image_path = Path(tmpdir) / "outfit.webp"
            image_path.write_bytes(b"webp-image")
            response = Mock(status_code=200)
            response.json.return_value = {
                "choices": [{"message": {"content": "视觉日程"}}],
            }
            with (
                patch("scheduler.llm_request_config", return_value={
                    "chat_url": "http://127.0.0.1:9999/chat/completions",
                    "api_key": "test-key",
                    "models": ["vision-model"],
                    "stream": False,
                }),
                patch("requests.post", return_value=response) as post,
            ):
                result = asyncio.run(scheduler._call_llm(
                    "请根据真人穿搭生成日程",
                    image_path=str(image_path),
                ))

        self.assertEqual("视觉日程", result)
        content = post.call_args.kwargs["json"]["messages"][0]["content"]
        self.assertEqual("text", content[0]["type"])
        self.assertEqual("image_url", content[1]["type"])
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/webp;base64,"))

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

    def test_all_schedule_prompt_variants_ask_for_semantic_variety_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = self.make_scheduler(tmpdir)
            today = date(2026, 7, 27)
            history = (
                "[2026-07-26]\n"
                "08:22 在阳台给绿植浇水并擦拭花盆\n"
                "12:48 到社区公园树荫下读小说\n"
                "19:46 在厨房煮番茄意面"
            )
            prompts = (
                scheduler._build_schedule_prompt(today, "（无）", history, ""),
                scheduler._build_compact_schedule_prompt(today, "（无）", history, ""),
                scheduler._build_emergency_schedule_prompt(today, history, "（无）", ""),
            )

        for prompt in prompts:
            self.assertIn("先把近 3 天每条日程归纳", prompt)
            self.assertIn("精彩锚点", prompt)
            self.assertIn("尽量让 6-8 条日程覆盖多种实质不同的动作族和场景", prompt)
            self.assertIn("逛多个商店", prompt)
            self.assertIn("最终自检", prompt)
            self.assertIn("这些是生成质量目标，不是生成后的拒绝条件", prompt)
            self.assertNotIn("生成时硬约束", prompt)
            self.assertNotIn("双保障", prompt)

    def test_generation_accepts_diversity_and_accessory_advisories(self):
        candidate = {
            "outfit_style": "清新风",
            "reference_query": "清爽自然的日常穿搭与城市生活氛围",
            "outfit": (
                "风格：清新风\n"
                "发型：低马尾配简洁发夹\n"
                "穿搭：蓝色棉质衬衫搭配白色直筒长裤和浅色运动鞋。\n"
                "动作：整理桌面上的活动材料\n"
                "场景：明亮的社区活动室"
            ),
            "schedule": "08:12 在社区活动室整理今天的材料",
            "schedule_prompt": "08:12 arrange today's materials in a bright community room",
            "schedule_details": [],
            "prompt": "adult woman arranging activity materials in a bright community room",
            "caption": "今天想按自己的节奏完成几件事，也给生活留一点新鲜感。",
            "photo_style_en": "Natural eye-level lifestyle photography in soft daylight.",
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = self.make_scheduler(tmpdir)
            outfit_reference = Path(tmpdir) / "xiaohongshu-outfit.webp"
            outfit_reference.write_bytes(b"reference")
            call_llm = AsyncMock(return_value=json.dumps(candidate, ensure_ascii=False))
            with (
                patch.object(scheduler, "_configured_today", return_value=date(2026, 7, 28)),
                patch.object(scheduler, "_call_llm", new=call_llm),
                patch.object(
                    scheduler,
                    "_validate_schedule_alignment",
                    return_value=([("08:12", "在社区活动室整理今天的材料")], [("08:12", "arrange materials")], ""),
                ),
                patch.object(scheduler, "_missing_required_periods", return_value=[]),
                patch.object(scheduler, "_valid_display_outfit", return_value=True),
                patch.object(scheduler, "_normalize_schedule_details", return_value=([], "")),
                patch.object(scheduler, "_schedule_forbidden_output_error", return_value=""),
                patch.object(
                    scheduler,
                    "_disliked_outfit_similarity_error",
                    return_value="与不喜欢的旧穿搭相似",
                ),
                patch.object(
                    scheduler,
                    "_schedule_diversity_error",
                    return_value="近 3 天任务主线重复",
                ) as diversity_note,
                patch.object(
                    scheduler,
                    "_outfit_accessory_repeat_error",
                    return_value="近 3 天已出现相同配饰",
                ) as accessory_note,
            ):
                entry = asyncio.run(scheduler.generate_today(
                    outfit_reference_path=str(outfit_reference),
                    xiaohongshu_search_query="夏季清新通勤穿搭",
                ))

        self.assertEqual("ok", entry.status)
        self.assertEqual("2026-07-28", entry.date)
        self.assertEqual("夏季清新通勤穿搭", entry.xiaohongshu_search_query)
        self.assertEqual(1, call_llm.await_count)
        llm_call = call_llm.await_args
        self.assertEqual(str(outfit_reference), llm_call.kwargs["image_path"])
        self.assertIn("小红书真人穿搭参考图", llm_call.args[0])
        self.assertIn("今日穿搭的唯一事实来源", llm_call.args[0])
        diversity_note.assert_called_once()
        accessory_note.assert_called_once()

    def test_xiaohongshu_keyword_is_selected_before_specific_garments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = self.make_scheduler(tmpdir)
            call_llm = AsyncMock(return_value='{"keyword":"夏季温柔居家穿搭"}')
            with (
                patch.object(scheduler, "_configured_today", return_value=date(2026, 7, 30)),
                patch.object(scheduler, "_get_history", return_value="（无历史记录）"),
                patch.object(scheduler, "_call_llm", new=call_llm),
            ):
                keyword = asyncio.run(scheduler.generate_xiaohongshu_search_query())

        self.assertEqual("夏季温柔居家穿搭", keyword)
        prompt = call_llm.await_args.args[0]
        self.assertIn("还没有生成今日日程", prompt)
        self.assertIn("不要设计具体衣服", prompt)
        self.assertEqual(True, call_llm.await_args.kwargs["json_mode"])

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

    def test_reports_one_retail_mainline_split_across_many_stores(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            schedule_data = {
                "2026-07-24": {
                    "status": "ok",
                    "schedule": (
                        "08:27 在书桌前列出今日购物清单\n"
                        "10:33 乘地铁前往市中心购物广场\n"
                        "14:45 在女装店试穿夏日新款\n"
                        "16:27 到家居区挑选收纳用品\n"
                        "18:52 回家整理新购衣物"
                    ),
                },
            }
            Path(tmpdir, "schedule_data.json").write_text(
                json.dumps(schedule_data, ensure_ascii=False),
                encoding="utf-8",
            )
            scheduler = self.make_scheduler(tmpdir)
            recent_counts = scheduler._recent_schedule_category_counts(date(2026, 7, 25))
            items = scheduler._schedule_plan_items(
                "08:34 到楼下咖啡窗口取一杯冰美式\n"
                "10:19 走进社区二手书店翻找室内设计杂志\n"
                "12:36 在巷口面馆吃一碗海鲜汤面\n"
                "14:22 到香氛小店试闻夏日木质调香水\n"
                "16:08 在文具店挑选新的活页本和中性笔\n"
                "18:41 到灯具专柜比较几款桌面阅读灯\n"
                "20:27 回家把新买的文具和灯具摆上书桌\n"
                "22:15 在床边准备明日直播提纲"
            )

            error = scheduler._schedule_diversity_error(
                items,
                recent_counts=recent_counts,
            )

            self.assertIn("全天主线过于单一", error)
            self.assertIn("购物/选购", error)
            self.assertIn("2026-07-24", error)
            self.assertIn("精彩锚点", error)

    def test_reports_repeated_secondary_reading_and_note_mainline(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            schedule_data = {
                "2026-07-26": {
                    "status": "ok",
                    "schedule": (
                        "12:48 在社区公园树荫长椅读轻小说\n"
                        "23:12 在床头写几句今日心情便签"
                    ),
                },
            }
            Path(tmpdir, "schedule_data.json").write_text(
                json.dumps(schedule_data, ensure_ascii=False),
                encoding="utf-8",
            )
            scheduler = self.make_scheduler(tmpdir)
            recent_counts = scheduler._recent_schedule_category_counts(date(2026, 7, 27))
            items = scheduler._schedule_plan_items(
                "08:17 在阳台练习基础瑜伽并记录身体状态\n"
                "10:38 到区图书馆挑选几本编程与设计书籍\n"
                "13:22 在轻食餐厅享用时令水果酸奶碗\n"
                "15:46 在图书馆阅读技术书并做笔记\n"
                "18:09 到创客空间参加树莓派入门工作坊\n"
                "20:33 回家煮蔬菜味噌汤配糙米饭\n"
                "22:18 在书房整理今天的学习笔记"
            )

            error = scheduler._schedule_diversity_error(
                items,
                recent_counts=recent_counts,
            )

            self.assertTrue(
                "全天主线过于单一" in error or "近 3 天任务主线重复" in error,
                error,
            )
            self.assertIn("阅读/记录/规划", error)
            self.assertIn("2026-07-26", error)

    def test_diagnostic_reports_recent_similar_actions(self):
        """The diagnostic can describe repetition without rejecting generation."""
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

    def test_prompt_asks_llm_to_judge_three_day_action_dedupe(self):
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
            self.assertIn("多样性参考", prompt)
            self.assertIn("去楼下便利店买气泡水", prompt)
            self.assertIn("只换说法、时间、地点、店铺或道具不算真正的新活动", prompt)
            self.assertIn("尽量避开相同或同义动作/任务主线", prompt)
            self.assertNotIn("双保障", prompt)

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
