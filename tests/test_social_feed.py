import json
import re
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

from social import REACTION_KINDS, SocialStore, SocialStoreCorruptError  # noqa: E402


class SocialStoreTest(unittest.TestCase):
    @staticmethod
    def _post_payload(text: str, **overrides) -> dict:
        payload = {
            "author_type": "character",
            "author_id": "zhuzhu",
            "author_snapshot": {
                "display_name": "猪猪",
                "avatar": "/api/social/media/avatar_11111111111111111111111111111111.jpg",
            },
            "text": text,
        }
        payload.update(overrides)
        return payload

    def test_posts_and_reply_tweets_persist_only_the_content_whitelist(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SocialStore(tmpdir)
            post = store.create_post(self._post_payload(
                "  今天去散步  ",
                media=[{
                    "filename": "social_22222222222222222222222222222222.png",
                    "url": "/images/private-local-path.png",
                    "alt": "  不应保存的本地提示词  ",
                }],
                source={"instance_id": "private-gallery-id"},
                private_prompt="不应保存",
            ))

            self.assertEqual("今天去散步", post["text"])
            self.assertEqual(
                "social_22222222222222222222222222222222.png",
                post["media"][0]["image_filename"],
            )
            self.assertEqual(
                "/api/social/media/social_22222222222222222222222222222222.png",
                post["media"][0]["image_url"],
            )
            self.assertNotIn("alt", post["media"][0])
            self.assertEqual(0, post["comment_count"])
            self.assertEqual(
                {kind: 0 for kind in REACTION_KINDS},
                post["reaction_counts"],
            )
            self.assertNotIn("reactions", post)

            comment_result = store.add_comment(post["id"], {
                "author_type": "user",
                "author_id": "user",
                "author_snapshot": {
                    "display_name": "我",
                    "avatar": "/api/social/media/avatar_33333333333333333333333333333333.jpg",
                    "private": "drop-me",
                },
                "instance_id": "private-gallery-id",
                "text": "  天气真好  ",
            })
            self.assertEqual("天气真好", comment_result["comment"]["text"])
            self.assertEqual(1, comment_result["post"]["comment_count"])

            persisted_before_reaction = Path(store.path).read_bytes()
            for kind in REACTION_KINDS:
                reaction = store.toggle_reaction(post["id"], kind, "  user:user  ")
                self.assertFalse(reaction["active"])
                self.assertTrue(reaction["local_only"])
            self.assertEqual(persisted_before_reaction, Path(store.path).read_bytes())

            reloaded = SocialStore(tmpdir).get_post(post["id"])
            self.assertIsNotNone(reloaded)
            self.assertEqual(1, reloaded["comment_count"])
            self.assertEqual([], reloaded["viewer_reactions"])
            self.assertEqual(
                {kind: 0 for kind in REACTION_KINDS},
                reloaded["reaction_counts"],
            )

            raw = json.loads(Path(store.path).read_text(encoding="utf-8"))
            self.assertEqual(
                {"version", "created_at", "updated_at", "posts"},
                set(raw),
            )
            stored_post = raw["posts"][0]
            self.assertEqual(
                {"id", "author_snapshot", "text", "media", "comments", "created_at"},
                set(stored_post),
            )
            self.assertEqual({"display_name", "avatar"}, set(stored_post["author_snapshot"]))
            self.assertEqual(
                {"type", "image_filename", "image_url"},
                set(stored_post["media"][0]),
            )
            self.assertEqual(
                {"id", "author_snapshot", "text", "created_at"},
                set(stored_post["comments"][0]),
            )
            serialized = json.dumps(raw, ensure_ascii=False)
            for forbidden in (
                "author_type",
                "author_id",
                "source",
                "reactions",
                "instance_id",
                "private_prompt",
                "private-gallery-id",
                "不应保存的本地提示词",
            ):
                self.assertNotIn(forbidden, serialized)

            deleted = store.delete_comment(
                post["id"], comment_result["comment"]["id"]
            )
            self.assertTrue(deleted["deleted"])
            self.assertEqual(0, deleted["post"]["comment_count"])

    def test_keyset_cursor_survives_new_and_deleted_posts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            post_ids = [f"post_{index:032x}" for index in range(1, 6)]
            posts = [
                self._post_payload(
                    f"post {index}",
                    id=post_ids[index - 1],
                    created_at=f"2026-07-31T0{index}:00:00+00:00",
                    updated_at=f"2026-07-31T0{index}:00:00+00:00",
                )
                for index in range(1, 6)
            ]
            (root / "social.json").write_text(
                json.dumps({"version": 1, "posts": posts}, ensure_ascii=False),
                encoding="utf-8",
            )
            store = SocialStore(tmpdir)

            first = store.list_posts(limit=2)
            self.assertEqual(
                [post_ids[4], post_ids[3]],
                [post["id"] for post in first["posts"]],
            )
            self.assertTrue(first["next_cursor"])

            store.create_post(self._post_payload("new top post"))
            store.delete_post(post_ids[3])
            second = store.list_posts(limit=2, before=first["next_cursor"])
            self.assertEqual(
                [post_ids[2], post_ids[1]],
                [post["id"] for post in second["posts"]],
            )
            self.assertTrue(second["next_cursor"])

            third = store.list_posts(limit=2, before=second["next_cursor"])
            self.assertEqual([post_ids[0]], [post["id"] for post in third["posts"]])
            self.assertEqual("", third["next_cursor"])

            with self.assertRaisesRegex(ValueError, "invalid_cursor"):
                store.list_posts(limit=2, before="not-a-valid-cursor")

    def test_remote_ownership_ids_are_unlinkable_but_still_deletable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SocialStore(tmpdir)
            owner = "gallery_" + "a" * 32
            other = "gallery_" + "b" * 32
            first = store.create_post(
                self._post_payload("first"),
                viewer_instance_id=owner,
            )
            second = store.create_post(
                self._post_payload("second"),
                viewer_instance_id=owner,
            )

            serialized = Path(store.path).read_text(encoding="utf-8")
            self.assertNotIn(owner, serialized)
            self.assertNotEqual(first["id"].split("_")[1], second["id"].split("_")[1])
            with self.assertRaisesRegex(PermissionError, "social_owner_required"):
                store.delete_post(first["id"], viewer_instance_id=other)
            self.assertTrue(
                store.delete_post(first["id"], viewer_instance_id=owner)["deleted"]
            )

    def test_legacy_owned_ids_remain_deletable_by_the_original_instance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            owner = "gallery_" + "c" * 32
            post_id = f"post_{owner}_{'d' * 32}"
            Path(tmpdir, "social.json").write_text(
                json.dumps({
                    "version": 2,
                    "posts": [{
                        "id": post_id,
                        "author_snapshot": {"display_name": "Legacy"},
                        "text": "legacy owned post",
                    }],
                }),
                encoding="utf-8",
            )
            store = SocialStore(tmpdir)
            migrated_post = store.list_posts(
                viewer_instance_id=owner,
            )["posts"][0]
            migrated_post_id = migrated_post["id"]

            self.assertNotEqual(post_id, migrated_post_id)
            self.assertRegex(migrated_post_id, r"^post_[0-9a-f]{32}_[0-9a-f]{32}$")
            self.assertTrue(migrated_post["can_delete"])
            self.assertFalse(
                store.get_post(
                    migrated_post_id,
                    viewer_instance_id="gallery_" + "e" * 32,
                )["can_delete"]
            )
            self.assertTrue(
                store.delete_post(migrated_post_id, viewer_instance_id=owner)["deleted"]
            )

    def test_cursor_order_is_stable_for_equal_timestamps_and_limit_boundaries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            timestamp = "2026-07-31T08:00:00+00:00"
            post_ids = [f"post_{character * 32}" for character in ("a", "c", "b")]
            posts = [
                self._post_payload(
                    post_id,
                    id=post_id,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                for post_id in post_ids
            ]
            (root / "social.json").write_text(
                json.dumps({"version": 1, "posts": posts}, ensure_ascii=False),
                encoding="utf-8",
            )
            store = SocialStore(tmpdir)

            first = store.list_posts(limit=0)
            self.assertEqual([post_ids[1]], [post["id"] for post in first["posts"]])
            second = store.list_posts(limit=1, before=first["next_cursor"])
            third = store.list_posts(limit=1, before=second["next_cursor"])
            self.assertEqual([post_ids[2]], [post["id"] for post in second["posts"]])
            self.assertEqual([post_ids[0]], [post["id"] for post in third["posts"]])
            self.assertEqual("", third["next_cursor"])

            default_page = store.list_posts(limit="invalid")
            self.assertEqual(3, len(default_page["posts"]))

    def test_dirty_json_is_normalized_without_unstable_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            dirty_posts = [
                {
                    "text": "legacy one",
                    "media": None,
                    "comments": [
                        {"post_id": "wrong-parent", "text": "first"},
                        {"post_id": "another-parent", "text": "second"},
                    ],
                    "reactions": {
                        "like": None,
                        "repost": "user:user",
                        "bookmark": [" user:user ", "user:user", ""],
                    },
                },
                {"text": "legacy two", "media": None, "comments": None},
                {"id": "duplicate", "text": "first duplicate"},
                {"id": "duplicate", "text": "second duplicate"},
                "not-a-post",
            ]
            path = root / "social.json"
            path.write_text(
                json.dumps({"version": 1, "posts": dirty_posts}, ensure_ascii=False),
                encoding="utf-8",
            )
            store = SocialStore(tmpdir)
            migrated_on_disk = path.read_text(encoding="utf-8")
            self.assertNotIn('"reactions"', migrated_on_disk)
            self.assertNotIn('"post_id"', migrated_on_disk)

            first_load = store.load()
            second_load = store.load()
            first_ids = [post["id"] for post in first_load["posts"]]
            second_ids = [post["id"] for post in second_load["posts"]]
            self.assertEqual(4, len(first_ids))
            self.assertEqual(4, len(set(first_ids)))
            self.assertTrue(all(
                re.fullmatch(r"post_[0-9a-f]{32}", post_id)
                for post_id in first_ids
            ))
            self.assertEqual(first_ids, second_ids)

            legacy = first_load["posts"][0]
            comment_ids = [comment["id"] for comment in legacy["comments"]]
            self.assertEqual(2, len(set(comment_ids)))
            self.assertTrue(all(
                re.fullmatch(r"comment_[0-9a-f]{32}", comment_id)
                for comment_id in comment_ids
            ))
            self.assertTrue(all("post_id" not in comment for comment in legacy["comments"]))
            self.assertNotIn("reactions", legacy)

            path.write_text('{"posts": null}', encoding="utf-8")
            with self.assertRaisesRegex(SocialStoreCorruptError, "social_store_corrupt"):
                store.load()

    def test_invalid_and_naive_timestamps_migrate_to_utc_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fallback = "2026-08-02T00:00:00+00:00"
            path = Path(tmpdir) / "social.json"
            path.write_text(
                json.dumps({
                    "version": 1,
                    "created_at": "not-a-timestamp",
                    "updated_at": "2026-07-31T08:00:00",
                    "posts": [{
                        "id": "post_" + "1" * 32,
                        "text": "naive timestamp",
                        "created_at": "2026-07-31T09:30:00",
                        "comments": [{
                            "id": "comment_" + "2" * 32,
                            "text": "invalid timestamp",
                            "created_at": "still-not-a-timestamp",
                        }],
                    }, {
                        "id": "post_" + "3" * 32,
                        "text": "offset timestamp",
                        "created_at": "2026-07-31T16:30:00+08:00",
                    }],
                }),
                encoding="utf-8",
            )

            with patch("social._now_iso", return_value=fallback):
                store = SocialStore(tmpdir)

            migrated = store.load()
            self.assertEqual(fallback, migrated["created_at"])
            self.assertEqual(fallback, migrated["updated_at"])
            self.assertEqual(fallback, migrated["posts"][0]["created_at"])
            self.assertEqual(
                fallback,
                migrated["posts"][0]["comments"][0]["created_at"],
            )
            self.assertEqual(
                "2026-07-31T08:30:00+00:00",
                migrated["posts"][1]["created_at"],
            )

    def test_corrupt_json_is_never_rewritten_as_an_empty_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "social.json"
            corrupt_bytes = b'{"posts": [{"text": "unfinished"}'
            path.write_bytes(corrupt_bytes)

            with self.assertRaisesRegex(SocialStoreCorruptError, "social_store_corrupt"):
                SocialStore(tmpdir)

            self.assertEqual(corrupt_bytes, path.read_bytes())

    def test_delete_only_changes_social_records_and_never_removes_images(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            image_path = root / "images" / "kept.png"
            image_path.parent.mkdir()
            image_path.write_bytes(b"image")
            group_chat_path = root / "group_chat.json"
            group_chat_path.write_text(
                '{"version":1,"rooms":{"room-1":{"name":"保留"}}}',
                encoding="utf-8",
            )
            group_chat_before = group_chat_path.read_bytes()
            store = SocialStore(tmpdir)
            target = store.create_post(self._post_payload(
                "with image",
                media=[{
                    "image_filename": "kept.png",
                    "image_url": "/images/kept.png",
                }],
            ))
            survivor = store.create_post(self._post_payload("keep this post"))
            comment = store.add_comment(target["id"], {"text": "reply"})["comment"]

            store.delete_comment(target["id"], comment["id"])
            self.assertTrue(image_path.is_file())
            self.assertEqual(group_chat_before, group_chat_path.read_bytes())

            deleted = store.delete_post(target["id"])
            self.assertTrue(deleted["deleted"])
            self.assertIsNone(store.get_post(target["id"]))
            self.assertIsNotNone(store.get_post(survivor["id"]))
            self.assertTrue(image_path.is_file())
            self.assertEqual(group_chat_before, group_chat_path.read_bytes())

            with self.assertRaises(KeyError):
                store.delete_post(target["id"])
            with self.assertRaises(KeyError):
                store.delete_comment("missing-post", "missing-comment")
            with self.assertRaisesRegex(ValueError, "comment_not_found"):
                store.delete_comment(survivor["id"], "missing-comment")

    def test_invalid_content_and_server_reactions_do_not_mutate_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SocialStore(tmpdir)
            with self.assertRaisesRegex(ValueError, "invalid_payload"):
                store.create_post(None)
            with self.assertRaisesRegex(ValueError, "content_required"):
                store.create_post({"text": " ", "media": [{"url": " "}]})

            post = store.create_post(self._post_payload("valid"))
            with self.assertRaisesRegex(ValueError, "invalid_payload"):
                store.add_comment(post["id"], None)
            with self.assertRaisesRegex(ValueError, "content_required"):
                store.add_comment(post["id"], {"text": "  "})
            with self.assertRaisesRegex(ValueError, "invalid_reaction"):
                store.toggle_reaction(post["id"], "clap", "user:user")
            with self.assertRaisesRegex(ValueError, "invalid_actor"):
                store.toggle_reaction(post["id"], "like", "  ")
            with self.assertRaises(KeyError):
                store.toggle_reaction("missing-post", "like", "user:user")

            before = Path(store.path).read_bytes()
            result = store.toggle_reaction(post["id"], "like", "user:user")
            self.assertTrue(result["local_only"])
            self.assertEqual(before, Path(store.path).read_bytes())

            unchanged = store.get_post(post["id"])
            self.assertEqual(0, unchanged["comment_count"])
            self.assertEqual(
                {kind: 0 for kind in REACTION_KINDS},
                unchanged["reaction_counts"],
            )

    def test_concurrent_creates_do_not_drop_posts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SocialStore(tmpdir)

            def create(index: int):
                return store.create_post(self._post_payload(f"post {index}"))

            with ThreadPoolExecutor(max_workers=8) as pool:
                created = list(pool.map(create, range(20)))

            persisted = SocialStore(tmpdir).list_posts(limit=100)["posts"]
            self.assertEqual(20, len(created))
            self.assertEqual(20, len(persisted))
            self.assertEqual(20, len({post["id"] for post in persisted}))

    def test_media_remote_url_whitelist_and_attach(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SocialStore(tmpdir)
            github_url = "https://raw.githubusercontent.com/i-kirito/picx-images-hosting/master/img/a.png"
            post = store.create_post(self._post_payload(
                "看图",
                media=[{
                    "image_filename": "social_33333333333333333333333333333333.png",
                    "remote_url": github_url,
                }],
            ))
            self.assertEqual(github_url, post["media"][0]["image_url"])
            self.assertEqual(github_url, post["media"][0]["remote_url"])

            bad = store.create_post(self._post_payload(
                "看图2",
                media=[{
                    "image_filename": "social_44444444444444444444444444444444.png",
                    "remote_url": "javascript:alert(1)",
                }],
            ))
            self.assertEqual(
                "/api/social/media/social_44444444444444444444444444444444.png",
                bad["media"][0]["image_url"],
            )
            self.assertNotIn("remote_url", bad["media"][0])

            store.attach_media_urls(post["id"], {
                "social_33333333333333333333333333333333.png": github_url,
            })
            fetched = store.get_post(post["id"], viewer_key="user:user", viewer_instance_id="")
            self.assertEqual(github_url, fetched["media"][0]["image_url"])
            self.assertEqual(github_url, fetched["media"][0]["remote_url"])


if __name__ == "__main__":
    unittest.main()
