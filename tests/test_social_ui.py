import unittest
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "app"


class SocialUiFrontendContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (APP_DIR / "web" / "index.html").read_text(encoding="utf-8")

    def test_gallery_has_all_four_social_post_entry_points(self):
        today_start = self.html.index("function renderToday()")
        today_end = self.html.index("// ===== Gallery Grid =====", today_start)
        today_card = self.html[today_start:today_end]

        gallery_start = self.html.index("function renderGallery()")
        gallery_end = self.html.index("// ===== Fullscreen Image Viewer =====", gallery_start)
        gallery = self.html[gallery_start:gallery_end]

        modal_start = self.html.index("modalOverlay.innerHTML = `")
        modal_end = self.html.index("document.body.appendChild(modalOverlay)", modal_start)
        detail_modal = self.html[modal_start:modal_end]

        entry_points = {
            "today_card": (
                today_card,
                "shareGalleryImageToSocial(${todayImageArg}, event)",
            ),
            "today_filter_card": (
                gallery,
                'class="tc-btn" onclick="shareGalleryImageToSocial(${imageFilenameArg}, event)"',
            ),
            "gallery_card": (
                gallery,
                'aria-label="发推" onclick="shareGalleryImageToSocial(${imageFilenameArg}, event)"',
            ),
            "detail_modal": (
                detail_modal,
                'id="modalSocialPost" onclick="shareCurrentModalImageToSocial(event)"',
            ),
        }
        for name, (section, marker) in entry_points.items():
            with self.subTest(entry_point=name):
                self.assertIn(marker, section)
                self.assertIn("发推", section)

    def test_social_feed_accepts_hub_media_proxy_urls(self):
        start = self.html.index("function socialMediaHtml(post)")
        end = self.html.index("function socialReactionButton", start)
        social_media = self.html[start:end]

        self.assertIn(r"\/api\/social\/media\/", social_media)
        self.assertIn("openGroupChatImage(${jsStringArg(item.image_url)})", social_media)

    def test_remote_posts_and_comments_hide_delete_actions(self):
        comment_start = self.html.index("function socialCommentHtml(post, comment)")
        comment_end = self.html.index("function socialMediaHtml(post)", comment_start)
        comment = self.html[comment_start:comment_end]
        post_start = self.html.index("function socialPostHtml(post)")
        post_end = self.html.index("function activeSocialHeartIcon", post_start)
        post = self.html[post_start:post_end]

        self.assertIn('comment?.can_delete === true ?', comment)
        self.assertIn("gcx-comment-delete", comment)
        self.assertIn('post?.can_delete === true ?', post)
        self.assertIn("gcx-post-delete", post)

    def test_avatars_use_the_media_proxy_and_reactions_stay_browser_local(self):
        avatar_start = self.html.index("function socialAvatarHtml(className, actor)")
        avatar_end = self.html.index("function socialReplyAuthorOptions", avatar_start)
        avatar = self.html[avatar_start:avatar_end]
        reaction_start = self.html.index("function toggleSocialReaction(postId, kind, button)")
        reaction_end = self.html.index("async function deleteSocialPost", reaction_start)
        reaction = self.html[reaction_start:reaction_end]

        self.assertIn(r"\/api\/social\/media\/", avatar)
        self.assertIn('galleryStorageSet("social_local_reactions"', reaction)
        self.assertNotIn("/api/social/posts/", reaction)
        self.assertNotIn("fetch(", reaction)

    def test_group_chat_contains_online_hub_settings_panel(self):
        start = self.html.index("<!-- Group Chat Page -->")
        end = self.html.index("<!-- Wardrobe Edit Modal -->", start)
        group_chat = self.html[start:end]

        for element_id in (
            "socialHubSettingsBtn",
            "socialHubSettings",
            "socialDisplayName",
            "socialHubUrl",
            "socialHubToken",
            "socialServerToken",
            "socialHubTestBtn",
            "socialHubSaveBtn",
            "socialGithubRepo",
            "socialGithubBranch",
            "socialGithubImagePath",
            "socialGithubToken",
            "socialGithubStatus",
        ):
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', group_chat)

        self.assertIn('onclick="toggleSocialHubSettings()"', group_chat)
        self.assertIn('onclick="testSocialHubConnection()"', group_chat)
        self.assertIn('onclick="saveSocialHubConfig()"', group_chat)
        self.assertIn("`${API}/api/social/config${query}`", self.html)
        self.assertIn("`${API}/api/social/config`", self.html)
        self.assertIn("`${API}/api/social/schedule-tweet`", self.html)
        self.assertIn("function buildScheduleTweetFallback", self.html)

    def test_publish_and_reply_require_a_valid_post_response(self):
        publish_start = self.html.index("async function createSocialPost()")
        publish_end = self.html.index("function toggleSocialReply", publish_start)
        publish = self.html[publish_start:publish_end]
        reply_start = self.html.index("async function createSocialComment(")
        reply_end = self.html.index("function toggleSocialReaction", reply_start)
        reply = self.html[reply_start:reply_end]

        self.assertIn('if (!data.post?.id) throw new Error("中心节点未返回有效动态")', publish)
        self.assertIn('if (!data.post?.id) throw new Error("中心节点未返回有效回复动态")', reply)


if __name__ == "__main__":
    unittest.main()
