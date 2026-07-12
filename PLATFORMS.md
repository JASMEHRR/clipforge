# PLATFORMS.md — Instagram / TikTok / Pinterest auto-posting (research, no build)

Research only, current as of July 2026. Decision document for the owner — nothing here is implemented. Companion to the YouTube auto-upload already shipped (`upload_scheduler.py`/`youtube_upload.py`).

Out of scope permanently: any Content-ID-evasion framing or feature. If cross-posting to these platforms is ever built, only honest re-encoding/reformatting — no evasion angle, ever.

## TikTok

**Official API — Content Posting API (Direct Post).**
- Account: any TikTok developer account + registered app; no special creator-account tier required, but the *app itself* must pass a TikTok audit before its posts can go public.
- What you get before the audit ("unaudited" tier): Direct Post works, but every post lands as `SELF_ONLY` (visible only to the creator) and the creator's account must be set to private at post time. You'd then have to manually flip both the account and the individual post to public in the TikTok app after the fact — that defeats the point of automation.
- After the audit: posts can go straight to public, subject to platform-wide caps (roughly 15 direct-post uploads per creator per 24h) and unaudited-tier caps (~5 users/24h) if you never pass audit.
- Cost: free API access; the audit is a compliance review of your app/ToS handling, not a paid tier, but it's a real review cycle (expect weeks, not days) and TikTok can reject an app with no explanation obligation.
- Effort: moderate-to-high. This is the only platform of the three with a real "your uploads are literally invisible until reviewed" gate for a personal project.

**Semi-official (Buffer/Later-class tools).**
- Buffer supports TikTok as a channel, but explicitly requires manual approval per post inside the TikTok app — Buffer stages, TikTok still makes the creator tap publish. Not true automation; same ceiling as the unaudited Direct Post API, just with a nicer UI.
- Cost: Buffer's per-channel pricing (~$5–12/channel/month) or similar Later-class tools. Not worth paying for something that still requires a manual tap.

**Unofficial.**
- Private/reverse-engineered TikTok upload clients exist (e.g. various `tiktok-uploader` style Python packages using session cookies or the app's internal API). Ban-risk data specific to TikTok is thinner than Instagram's, but TikTok has aggressively fingerprinted and rate-limited unofficial clients in recent years; expect similar risk category to Instagram's unofficial tools (double-digit-percent annual suspension risk for automation-pattern accounts) rather than the sub-1% official-API rate.
- Given it's a real creator account (not a throwaway), this is not worth the risk for a few Shorts a day.

**Recommendation: build later, manual-stage now.** The audit gate makes "build now" pointless until upload volume justifies weeks of review latency. ClipForge should generate the TikTok-flavored caption/hashtag text and put the rendered file in a "ready to post" folder/panel for one-tap manual TikTok upload today; revisit the Direct Post API once volume is consistent enough to justify submitting for audit.

## Instagram (Reels)

**Official API — Graph API, `instagram_business_content_publish` permission.**
- Account: **Instagram Business account required** (linked to a Facebook Page). Creator accounts are explicitly NOT supported for API publishing — this is a hard account-type requirement, not a config toggle. If the owner's current account is a Creator account, it needs conversion to Business first (free, but changes some IG-native features like certain creator tools/insights framing).
- App review: each permission (`instagram_business_basic`, `instagram_business_content_publish`) needs its own Meta App Review submission with a screencast demoing the full flow. Expect 2–4 weeks per round, and rejections send you back to the queue.
- What's allowed: publish a Reel (create a media container, then publish it) — 9:16, 5–90 seconds, H.264/HEVC. **Note the ceiling:** Instagram's native app now allows Reels up to 3 minutes, but the Graph API is still capped at 90 seconds — if ClipForge ever produces longer-form Shorts, the API can't post them regardless of review status.
- Scheduling: the API supports creating+publishing in one call; true "publish at a future time" scheduling is more limited than YoutubeAPI's `publishAt` — most tools fake scheduling by holding the file and firing the publish call at the right time (client-side timer), same as ClipForge would do for YouTube's non-scheduled path if publishAt weren't available. First-comment posting is possible via a separate comment-create call after publish.
- Rate limit: ~100 API-published posts per rolling 24h — a total non-issue at ClipForge's volume.
- Cost: free (Meta developer account + app).

**Semi-official (Buffer/Later-class).**
- Buffer/Later support Instagram Reels scheduling through the same underlying Graph API — i.e., they've already done the app-review work for you, at the cost of a monthly per-channel fee (~$5-12/mo) and being one more dependency in the pipeline. Only worth it if the owner doesn't want to run Meta's app-review process personally.

**Unofficial (instagrapi-style private-API clients).**
- Real, well-documented, actively maintained (e.g. `instagrapi`). But ban-risk data is stark: official-API-based automation tools see well under 1% annual suspension; unofficial/session-based automation tools land in the 15-30%/year range — roughly an order of magnitude+ riskier. Meta has been tightening device-fingerprinting and challenge triggers through 2025-2026 specifically against this pattern.
- For a personal/creator's real account, this is a bad trade for a few automated Reel posts a day. Do not build this.

**Recommendation: build now, but as a smaller lift than TikTok's.** No blocking audit — just the standard 2-4 week Meta App Review, and it's a one-time cost. Real ceiling to flag: the 90s Reels cap means some ClipForge output (if clip length ever exceeds 90s) can't go through this API at all. Converting the account to Business is a prerequisite the owner needs to do manually before any of this can start.

## Pinterest

**Official API — Pinterest API v5.**
- Account: any Pinterest account works for the API itself; a business/creator distinction matters less here than on IG/TikTok.
- Access tiers: apps get **Trial access** after initial review (fast, ~1-2 business days) — but Trial-created Pins are hidden from the public (your code works, the Pin ID comes back, nobody outside your own app can see it — same "invisible until reviewed" trap as TikTok's unaudited tier, just faster to get to and a smaller ask). **Standard access** (real, publicly visible Pins) needs a second review round, ~1-4 weeks, requiring a demo video of the full OAuth flow plus a real pin-create call.
- What's allowed: video Pins are fully supported via a two-step flow (upload to `/v5/media`, poll until processed, then `POST /v5/pins` with the resulting media ID, board ID, cover image, title/description). More moving parts than YouTube/IG but no creative restriction beyond that.
- Cost: free API access; the two-tier review is time cost only.
- Effort: lowest of the three platforms for a personal project — no account-type conversion needed, and the "hard" review (Standard) only needs one clean demo video, not per-permission demos like Meta's.

**Semi-official (Buffer-class).**
- Buffer lists Pinterest as a supported channel. Same per-channel pricing tradeoff as Instagram — worth it only if the owner wants to skip doing Pinterest's own review personally.

**Unofficial.**
- Little documented ban-risk data specific to Pinterest automation (it's a much lower-stakes platform for account bans than IG/TikTok — Pinterest doesn't have the same anti-bot enforcement reputation). Still not recommended given the official path is comparatively easy to get through.

**Recommendation: build now.** Of the three, Pinterest has the shortest realistic path from zero to a real automated Standard-access pin post — no account conversion, no per-permission review multiplication, one demo video. Good candidate to build first if the owner wants a working auto-cross-post proof of concept before tackling Instagram's longer review or TikTok's audit gate.

## Phased plan

1. **Now:** Extend ClipForge's existing metadata generation (title/description/hashtags, already built for YouTube) to also emit Pinterest- and Instagram/TikTok-flavored variants (different hashtag conventions, Pinterest's title+description+board fields), and stage the rendered file + copy in a "ready to cross-post" panel for one-tap manual posting on all three platforms. This alone removes most of the manual toil regardless of what gets automated later.
2. **Next (build):** Pinterest API v5 integration — shortest review path, full automation reachable in weeks not months.
3. **Then (build):** Instagram Graph API — requires the owner to convert to a Business account first, then a 2-4 week Meta App Review; automate once approved. Flag the 90s Reels cap as a real ceiling if clip lengths ever grow past it.
4. **Later, only if volume justifies it:** TikTok Content Posting API — submit for audit once upload cadence is established (submitting early with low/no post history doesn't help and burns a review cycle); until then, keep using the manual-staging panel from step 1.
5. **Never:** unofficial/private-API clients for any of the three on a real creator account — the ban-risk data (order-of-magnitude higher suspension rates for unofficial automation vs. official APIs) isn't worth trading against the channel itself.

---
Sources consulted (blog/aggregator content on official docs, cross-checked against the platforms' own developer documentation where linked):
- [TikTok Content Posting API Guide](https://developers.tiktok.com/doc/content-posting-api-reference-direct-post)
- [TikTok Content Sharing Guidelines](https://developers.tiktok.com/doc/content-sharing-guidelines)
- [TikTok Content Posting API in 2026 — PostPeer](https://www.postpeer.dev/blog/best-tiktok-posting-api)
- [Instagram Reels API Publishing Guide (2026) — Postproxy](https://postproxy.dev/blog/instagram-reels-api-publishing-guide/)
- [Instagram Reels API: Complete Developer Guide (2026) — Phyllo](https://www.getphyllo.com/post/a-complete-guide-to-the-instagram-reels-api)
- [Pinterest Create Pin — official docs](https://developers.pinterest.com/docs/api/v5/pins-create/)
- [Pinterest API Posting Integration Guide (2026) — Postproxy](https://postproxy.dev/blog/pinterest-api-posting-integration-guide/)
- [Buffer Pricing 2026 — Social Champ](https://www.socialchamp.com/blog/buffer-pricing/)
- [Which Social Media APIs Support Multi-Platform Posting — Buffer](https://buffer.com/resources/social-media-api-multi-platform-posting/)
- [Is There a Real Risk of Getting Banned Using instagrapi — GitHub Discussion](https://github.com/subzeroid/instagrapi/discussions/2224)
- [Instagram Automation Ban Risk: The Truth — PostEngage.ai](https://postengage.ai/blog/instagram-automation-ban-risk-truth)

Note: several of these are third-party SEO/aggregator sites rather than the platforms' own docs, so treat exact numeric figures (rate limits, suspension percentages, review timelines) as directional, not contractual — re-verify against Meta/TikTok/Pinterest's own developer docs immediately before building against any of these.
