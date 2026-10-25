# Publishing findings

Checked 25 October 2026 against the official pages linked below. This app calls a platform only through the flow that page still documents, and only after you put credentials in `.env` and click publish. Nothing is posted during tests.

## Instagram

Official Content Publishing exists on a free Meta developer app. It is not a paid API.

- Overview: https://developers.facebook.com/docs/instagram-platform/overview
- Content publishing: https://developers.facebook.com/docs/instagram-platform/content-publishing/
- Facebook Login variant (page updated 30 June 2026): https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/content-publishing/

Flow the adapter implements:

1. `POST https://graph.facebook.com/{version}/{ig-user-id}/media` with `image_url` and `caption`.
2. `POST https://graph.facebook.com/{version}/{ig-user-id}/media_publish` with `creation_id`.

Permissions:

- Facebook Login: `instagram_basic`, `instagram_content_publish`, `pages_read_engagement`, plus a Page task of `CREATE_CONTENT` or `PROFILE_PLUS_CREATE_CONTENT`.
- Instagram Login: `instagram_business_basic` and `instagram_business_content_publish`.

Constraints this app enforces:

- A professional Instagram account is required, usually linked to a Facebook Page.
- The image must already be on a public `https` URL. Meta fetches it. This app does not host public files, so a missing or non-https image stays in the manual queue and no request is sent.
- JPEG only, according to the content-publishing page. Text-only posts are not in the official API, so the adapter refuses them.
- Captions longer than 2200 characters are refused locally.
- Rate-limit pages disagree: an older page said 50 API posts per 24 hours, and the 30 June 2026 page said 100. This app uses the stricter local cap of 50 (`INSTAGRAM_DAILY_CAP`).
- Default Graph version is `v26.0` (`META_GRAPH_VERSION`), accepted only when it matches `v<digits>.<digits>`.

A post is marked published only when the publish call returns HTTP 200, no `error` object, and an `id`.

## Facebook Pages

Official text posts exist.

- Page feed reference: https://developers.facebook.com/docs/graph-api/reference/page/feed/
- Pages API getting started: https://developers.facebook.com/documentation/pages-api/getting-started
- Posts: https://developers.facebook.com/documentation/pages-api/posts

Flow: `POST https://graph.facebook.com/v26.0/{page-id}/feed` with `message` and a Page access token.

Permissions: `pages_manage_posts`, `pages_read_engagement`, `pages_show_list`. The user needs the `CREATE_CONTENT` task on the Page.

Success body is `{"id": "<page_post_id>"}`. The Page id must be digits. No daily cap is invented; the official pages above do not state one that this app should enforce.

## Naver Blog

The Blog write Open API was terminated on 6 May 2020.

- Notice: https://developers.naver.com/notice/article/7527
- Blog People notice: https://m.blog.naver.com/blogpeople/221893702144
- Blog URL scheme ended 23 December 2022: https://developers.naver.com/notice/article/8595
- Current API list (blog write is gone, cafe write remains): https://developers.naver.com/docs/common/openapiguide/apilist.md

The adapter is `automated = False`. The UI offers prepared text, hashtags, and export. It never sends an HTTP publish request for Naver Blog.

## Naver Cafe

The Cafe write API is still documented and is free with Naver Login.

- API guide: https://developers.naver.com/docs/login/cafe-api/cafe-api.md
- Product page: https://developers.naver.com/products/login/cafe/cafe.md
- Limits: https://developers.naver.com/products/intro/plan/plan.md

Flow: `POST https://openapi.naver.com/v1/cafe/{clubid}/menu/{menuid}/articles` with `Authorization: Bearer` and a form body.

Limits and constraints:

- 200 posts per day per Naver account. The local cap defaults to 200. The join API limit of 50 per day is a different API and is not implemented. This app does not join cafes.
- Club id and menu id must be digits.
- Product boards are not supported by the write API.
- Current docs say to take the UTF-8 text, re-encode it as MS949, then percent-encode those bytes. The adapter does `value.encode("ms949")` and then `%XX` uppercase. The Hangul syllable 가 becomes `%B0%A1`.
- Success is HTTP 200 and `message.status == "200"` and `message.result.articleId`. Example: `{"message":{"status":"200","result":{"msg":"Success","cafeUrl":"...","articleId":4,"articleUrl":"https://cafe.naver.com/.../4"}}}`.
- Cafe rules may forbid promotion. You still have to click publish.

## What is not used

Naver Search API is not used. New developer-center search apps closed on 31 July 2026, and the migration path is NAVER API HUB on NCP, which is temporarily free and paid later. That is a hosted service, so this app does not call it. Notice: https://developers.naver.com/notice/article/32530

Community research uses two public lesson RSS feeds (BBC Learning English 6 Minute English and VOA Learning English) plus manual URL import. Those feeds are topic sources, not comment boxes. Replies are drafts for you to copy. There is no submit, comment, or message function.

## Idempotency

Meta and Naver do not offer a general idempotency key for these write calls. Each queue item has a local UUID. After a confirmed success the API is not called again. A status of `publishing` blocks a second call. A timeout, HTTP 5xx, HTTP 429, or an HTTP 200 without an id sets `retry_blocked` until you confirm on the platform that the post is not there. A clear HTTP 4xx can be retried. The database refuses `published` unless `confirmed_success` is set and `external_id` is non-empty. The service refuses any transition that would mark published without that confirmation.

## Engagement

Stored engagement comes only from an official API payload:

- Facebook: `reactions.summary.total_count` and `comments.summary.total_count`.
- Instagram: `like_count` and `comments_count`.

A missing field is not stored as zero. The table accepts only `source = 'official_api'`. With no credentials, no published post, or an empty payload, the page says `not available`. Naver engagement is not implemented. Refresh uses GET query parameters and does not write the token into the log. Tokens are redacted from stored attempt text.
