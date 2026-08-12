import json
import sys
import tempfile
import unittest
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from keyword_cloud import (  # noqa: E402
    build_keyword_cloud_payload,
    build_schedule_keyword_prompt_block,
    hide_keyword_cloud_term,
)


class KeywordCloudTest(unittest.TestCase):
    def test_counts_user_input_keywords_and_ignores_generated_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            schedule_data = {
                "custom_prompt_1000.png": {
                    "status": "ok",
                    "source": "custom",
                    "image_filename": "custom_prompt_1000.png",
                    "custom_prompt": "红色开衫，百褶裙，咖啡店",
                    "outfit_keywords": "generated cardigan, generated skirt",
                    "scene_keywords": "generated cafe, generated light",
                    "prompt": "She is wearing generated cardigan. Background: generated cafe.",
                },
                "2026-07-06": {
                    "status": "ok",
                    "custom_prompt": "ignored daily schedule keyword",
                    "prompt": "This date-key schedule is not an image call.",
                },
            }
            metadata = {
                "custom_prompt_1000.png": {
                    "source": "custom",
                    "custom_prompt": "红色开衫，百褶裙，咖啡店",
                    "outfit_keywords": "metadata generated cardigan",
                    "scene_keywords": "metadata generated cafe",
                },
                "hermes_1001.png": {
                    "source": "hermes_api",
                    "user_prompt": "红色开衫，白色运动鞋，咖啡店",
                    "outfit_keywords": "hermes generated cardigan",
                    "scene_keywords": "hermes generated cafe",
                    "prompt": "Generated final prompt that should not be counted.",
                },
            }
            (data_dir / "schedule_data.json").write_text(
                json.dumps(schedule_data, ensure_ascii=False),
                encoding="utf-8",
            )
            (data_dir / "image_metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False),
                encoding="utf-8",
            )

            payload = build_keyword_cloud_payload(str(data_dir), limit=10)
            by_text = {item["text"]: item for item in payload["keywords"]}

            self.assertEqual("user_input_and_favorite_wardrobe", payload["basis"])
            self.assertEqual(2, by_text["红色开衫"]["count"])
            self.assertEqual(2, by_text["咖啡店"]["count"])
            self.assertNotIn("ignored daily schedule keyword", by_text)
            self.assertNotIn("generated cardigan", by_text)
            self.assertNotIn("generated cafe", by_text)
            self.assertNotIn("Generated final prompt that should not be counted", by_text)
            source_labels = {
                source["label"]
                for source in by_text["红色开衫"]["sources"]
            }
            self.assertIn("Hermes", source_labels)
            self.assertIn("自定义", source_labels)

    def test_excludes_automatic_schedule_entries_including_manual_edits(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            (data_dir / "schedule_data.json").write_text(
                json.dumps({
                    "zhuzhu_custom_cron.png": {
                        "status": "ok",
                        "source": "cron",
                        "image_filename": "zhuzhu_custom_cron.png",
                        "custom_prompt": "改成繁华商业街，黑色皮夹克",
                        "user_prompt": (
                            "fair skin, balanced facial features, natural waistline, "
                            "props, setting, mood, time of day"
                        ),
                        "prompt": "This image should look photorealistic. Background: generated cron scene.",
                    },
                    "schedule_web.png": {
                        "status": "ok",
                        "source": "web",
                        "image_filename": "schedule_web.png",
                        "user_prompt": (
                            "fair skin, balanced facial features, natural waistline, "
                            "props, setting, mood, time of day"
                        ),
                        "prompt": "Generated final web prompt.",
                    },
                }, ensure_ascii=False),
                encoding="utf-8",
            )

            payload = build_keyword_cloud_payload(str(data_dir), limit=20)
            by_text = {item["text"]: item for item in payload["keywords"]}

            self.assertEqual(2, payload["scanned_count"])
            self.assertEqual(0, payload["entry_count"])
            self.assertEqual([], payload["keywords"])
            self.assertNotIn("改成繁华商业街", by_text)
            self.assertNotIn("黑色皮夹克", by_text)
            self.assertNotIn("fair skin", by_text)
            self.assertNotIn("balanced facial features", by_text)
            self.assertNotIn("natural waistline", by_text)
            self.assertNotIn("props", by_text)
            self.assertNotIn("setting", by_text)
            self.assertNotIn("mood", by_text)
            self.assertNotIn("time of day", by_text)

    def test_prefers_explicit_hermes_input_and_filters_template_fragments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            (data_dir / "image_metadata.json").write_text(
                json.dumps({
                    "hermes_custom.png": {
                        "source": "hermes_api",
                        "custom_prompt": (
                            "silver bob haircut, amber eyes, freckled cheeks, rainy bookstore, "
                            "fair skin, balanced facial features, highly detailed, best quality"
                        ),
                        "user_prompt": (
                            "fair skin, balanced facial features, detailed fabric textures, "
                            "same cut, do not crop the head"
                        ),
                    }
                }, ensure_ascii=False),
                encoding="utf-8",
            )

            payload = build_keyword_cloud_payload(str(data_dir), limit=20)
            by_text = {item["text"]: item for item in payload["keywords"]}

            self.assertIn("silver bob haircut", by_text)
            self.assertIn("amber eyes", by_text)
            self.assertIn("freckled cheeks", by_text)
            self.assertIn("rainy bookstore", by_text)
            self.assertNotIn("fair skin", by_text)
            self.assertNotIn("balanced facial features", by_text)
            self.assertNotIn("highly detailed", by_text)
            self.assertNotIn("best quality", by_text)
            self.assertNotIn("detailed fabric textures", by_text)
            self.assertNotIn("same cut", by_text)
            self.assertNotIn("do not crop the head", by_text)

    def test_counts_favorite_wardrobe_without_wardrobe_prompt_template(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            (data_dir / "favorite_outfits.json").write_text(
                json.dumps({
                    "items": [
                        {
                            "id": "fav_1",
                            "outfit_style": "复古风",
                            "outfit_keywords": "black silk camisole, lace cardigan",
                            "outfit": {
                                "风格": "复古风",
                                "发型": "低侧马尾，黑色缎带",
                                "穿搭": "黑色真丝吊带背心，蕾丝开衫，黑色短裤",
                            },
                            "wardrobe_image": {
                                "filename": "wardrobe_1.png",
                                "prompt": "A clean wardrobe catalog photo, no person, no body parts.",
                            },
                        }
                    ]
                }, ensure_ascii=False),
                encoding="utf-8",
            )

            payload = build_keyword_cloud_payload(str(data_dir), limit=20)
            by_text = {item["text"]: item for item in payload["keywords"]}

            self.assertIn("black silk camisole", by_text)
            self.assertIn("蕾丝开衫", by_text)
            self.assertIn("低侧马尾", by_text)
            self.assertNotIn("clean wardrobe catalog photo", by_text)
            source_labels = {
                source["label"]
                for source in by_text["蕾丝开衫"]["sources"]
            }
            self.assertIn("收藏衣柜", source_labels)

    def test_extracts_jk_alias_from_prompt_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            (data_dir / "image_metadata.json").write_text(
                json.dumps({
                    "recovered_prompt_1002.png": {
                        "source": "recovered",
                        "prompt": (
                            "This image should look like a high-quality raw photo captured on a flagship smartphone. "
                            "Masterpiece clarity, hyper realistic. She is wearing classic JK uniform with navy "
                            "pleated mini skirt and white sailor blouse with red ribbon. "
                            "Background: sunlit school corridor."
                        ),
                    },
                    "recovered_prompt_1003.png": {
                        "source": "recovered",
                        "prompt": "穿着白色水手服JK制服和海军蓝百褶裙，背着书包站在学校门口",
                    },
                }, ensure_ascii=False),
                encoding="utf-8",
            )

            payload = build_keyword_cloud_payload(str(data_dir), limit=20)
            by_text = {item["text"]: item for item in payload["keywords"]}

            self.assertEqual(2, by_text["JK制服"]["count"])
            self.assertNotIn("masterpiece clarity", by_text)
            self.assertNotIn("high-quality raw photo captured on a flagship smartphone", by_text)
            self.assertNotIn("sunlit school corridor", by_text)

    def test_schedule_prompt_block_contains_soft_reference_terms(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            (data_dir / "schedule_data.json").write_text(
                json.dumps({
                    "hermes_1001.png": {
                        "status": "ok",
                        "source": "hermes_api",
                        "image_filename": "hermes_1001.png",
                        "custom_prompt": "蓝色卫衣，帆布包，书店过道",
                        "outfit_keywords": "generated blue hoodie, generated canvas tote",
                        "scene_keywords": "generated bookstore aisle",
                    }
                }, ensure_ascii=False),
                encoding="utf-8",
            )

            block = build_schedule_keyword_prompt_block(str(data_dir), limit=5)

            self.assertIn("软偏好参考", block)
            self.assertIn("用户手动输入", block)
            self.assertIn("收藏衣柜", block)
            self.assertIn("最多自然采用 1 个", block)
            self.assertIn("也可以全部忽略", block)
            self.assertIn("蓝色卫衣", block)
            self.assertIn("书店过道", block)
            self.assertNotIn("generated blue hoodie", block)
            self.assertNotIn("(1)", block)

    def test_schedule_prompt_block_ignores_automatic_template_noise(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            (data_dir / "schedule_data.json").write_text(
                json.dumps({
                    "schedule_cron.png": {
                        "status": "ok",
                        "source": "cron",
                        "image_filename": "schedule_cron.png",
                        "user_prompt": (
                            "fair skin, balanced facial features, natural waistline, "
                            "props, setting, mood, outfit, hairstyle, time of day"
                        ),
                    },
                    "custom_real.png": {
                        "status": "ok",
                        "source": "custom",
                        "image_filename": "custom_real.png",
                        "custom_prompt": "蓝色卫衣，帆布包，书店过道",
                    },
                }, ensure_ascii=False),
                encoding="utf-8",
            )

            block = build_schedule_keyword_prompt_block(str(data_dir), limit=5)

            self.assertIn("蓝色卫衣", block)
            self.assertIn("帆布包", block)
            self.assertIn("书店过道", block)
            self.assertNotIn("fair skin", block)
            self.assertNotIn("balanced facial features", block)
            self.assertNotIn("natural waistline", block)
            self.assertNotIn("time of day", block)

    def test_schedule_prompt_rotates_and_caps_low_weight_candidates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            schedule_data = {
                f"custom_{index:02d}.png": {
                    "status": "ok",
                    "source": "custom",
                    "image_filename": f"custom_{index:02d}.png",
                    "custom_prompt": f"偏好词{index:02d}",
                }
                for index in range(20)
            }
            (data_dir / "schedule_data.json").write_text(
                json.dumps(schedule_data, ensure_ascii=False),
                encoding="utf-8",
            )

            first = build_schedule_keyword_prompt_block(
                str(data_dir),
                limit=20,
                selection_key="2026-07-15",
            )
            second = build_schedule_keyword_prompt_block(
                str(data_dir),
                limit=20,
                selection_key="2026-07-16",
            )

            first_terms = first.split("可选灵感：", 1)[1].split("、")
            second_terms = second.split("可选灵感：", 1)[1].split("、")
            self.assertEqual(5, len(first_terms))
            self.assertEqual(5, len(second_terms))
            self.assertNotEqual(first_terms, second_terms)
            self.assertNotIn("(", first.split("可选灵感：", 1)[1])

    def test_keeps_user_supplied_character_traits_as_preferences(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            (data_dir / "image_metadata.json").write_text(
                json.dumps({
                    "custom_traits.png": {
                        "source": "custom",
                        "user_prompt": "silver bob haircut, amber eyes, freckled cheeks, original-character-alpha",
                    }
                }, ensure_ascii=False),
                encoding="utf-8",
            )

            payload = build_keyword_cloud_payload(str(data_dir), limit=10)
            by_text = {item["text"]: item for item in payload["keywords"]}

            self.assertIn("silver bob haircut", by_text)
            self.assertIn("amber eyes", by_text)
            self.assertIn("freckled cheeks", by_text)
            self.assertIn("original-character-alpha", by_text)

    def test_hidden_keyword_persists_and_is_removed_from_schedule_hints(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            (data_dir / "schedule_data.json").write_text(
                json.dumps({
                    "custom_1.png": {
                        "status": "ok",
                        "source": "custom",
                        "image_filename": "custom_1.png",
                        "custom_prompt": "红色开衫，咖啡店",
                    },
                    "custom_2.png": {
                        "status": "ok",
                        "source": "custom",
                        "image_filename": "custom_2.png",
                        "custom_prompt": "红色开衫，书店过道",
                    },
                }, ensure_ascii=False),
                encoding="utf-8",
            )

            self.assertIn(
                "红色开衫",
                {item["text"] for item in build_keyword_cloud_payload(str(data_dir))["keywords"]},
            )

            hidden = hide_keyword_cloud_term(str(data_dir), "红色开衫")
            payload = build_keyword_cloud_payload(str(data_dir))
            prompt_block = build_schedule_keyword_prompt_block(str(data_dir), limit=5)
            preferences = json.loads(
                (data_dir / "keyword_cloud_preferences.json").read_text(encoding="utf-8")
            )

            self.assertEqual("红色开衫", hidden["hidden_keyword"])
            self.assertEqual(1, hidden["hidden_count"])
            self.assertEqual(1, payload["hidden_count"])
            self.assertNotIn("红色开衫", {item["text"] for item in payload["keywords"]})
            self.assertNotIn("红色开衫", prompt_block)
            self.assertEqual(["红色开衫"], preferences["hidden_keywords"])

    def test_hidden_keyword_is_exact_idempotent_and_backfills_ranked_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            (data_dir / "schedule_data.json").write_text(
                json.dumps({
                    "custom_1.png": {
                        "status": "ok",
                        "source": "custom",
                        "image_filename": "custom_1.png",
                        "custom_prompt": "red coat, red coat belt, blue scarf",
                    },
                    "custom_2.png": {
                        "status": "ok",
                        "source": "custom",
                        "image_filename": "custom_2.png",
                        "custom_prompt": "red coat, red coat belt",
                    },
                    "custom_3.png": {
                        "status": "ok",
                        "source": "custom",
                        "image_filename": "custom_3.png",
                        "custom_prompt": "red coat",
                    },
                }, ensure_ascii=False),
                encoding="utf-8",
            )

            first = build_keyword_cloud_payload(str(data_dir), limit=1)
            self.assertEqual("red coat", first["keywords"][0]["text"])

            hide_keyword_cloud_term(str(data_dir), "  RED   COAT ")
            hidden_again = hide_keyword_cloud_term(str(data_dir), "red coat")
            payload = build_keyword_cloud_payload(str(data_dir), limit=1)
            preferences = json.loads(
                (data_dir / "keyword_cloud_preferences.json").read_text(encoding="utf-8")
            )

            self.assertEqual(1, hidden_again["hidden_count"])
            self.assertEqual(["red coat"], preferences["hidden_keywords"])
            self.assertEqual("red coat belt", payload["keywords"][0]["text"])
            self.assertEqual(1, payload["hidden_count"])


if __name__ == "__main__":
    unittest.main()
