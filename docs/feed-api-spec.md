# Feed API spec — build an endpoint a cron poster consumes

> Self-contained spec. Hand this whole file to a builder agent. It does **not** need any
> other file in this repo — it only needs to produce the HTTP response described here.

## Goal

Build an HTTP `GET` endpoint that returns a **JSON Feed**. A separate scheduled job polls
this URL on a cron (e.g. hourly), takes the **newest item it hasn't already posted**, and
publishes it to Threads (a 500-char social platform). You are building **only the feed
endpoint** — not the poster.

You choose where items come from (a database, a generator, an LLM, an RSS mirror, etc.).
The only thing that's fixed is the **response shape and its semantics** below.

## Response contract

- Method: `GET`. Status `200`.
- `Content-Type: application/json` (the body MUST start with `{`).
- Body: a JSON object with an `items` array, newest-first preferred.

```json
{
  "version": "https://jsonfeed.org/version/1.1",
  "title": "Threads cron source",
  "items": [
    {
      "id": "2026-06-04-001",
      "title": "This entire string becomes the Threads post body.",
      "url": "https://example.com/permalink/1",
      "date_published": "2026-06-04T10:00:00Z"
    }
  ]
}
```

### Fields the consumer reads (everything else is ignored)

| Field | Required | Meaning — read this carefully |
| --- | --- | --- |
| `items[].title` | **yes** | **The literal post text.** The consumer posts `title`, then a blank line, then `url`. `content_text` / `content_html` / `summary` are **NOT** read — put the post text in `title`. |
| `items[].url` | recommended | Appended under the title as a link. Omit it → the post is just `title` with no link. |
| `items[].id` | **strongly recommended** | The **de-duplication key**. Must be **stable and unique per logical item**. If omitted, the consumer derives the key from `SHA-256(url + " " + title)`. |
| `items[].date_published` | recommended | ISO-8601 timestamp. The consumer sorts items newest-first by this and posts the newest unseen one. If omitted from all items, array order is used (so put newest first). |

## Hard constraints (these cause real bugs if violated)

1. **One item is posted per poll** — the consumer posts the single newest unseen item, not
   the whole feed. Returning many items is fine; they get posted one cron tick at a time.
2. **`id` MUST be stable.** If the same logical item's `id` changes between polls, it gets
   **reposted**. If you omit `id`, then any change to `title` or `url` reposts it (the hash
   changes). For generated content, emit a deterministic `id` (content hash, or a sequence
   you persist).
3. **`title` is the literal post.** Keep it within **500 characters** including the appended
   `\n\n<url>`; the consumer hard-truncates to 500 with an ellipsis, so budget ~440 chars of
   title if you include a ~50-char URL.
4. **Valid JSON, object root.** Must parse and start with `{`. An empty feed is valid:
   `{"version":"https://jsonfeed.org/version/1.1","title":"...","items":[]}` → the consumer
   simply posts nothing that tick.

## Acceptance checklist

- [ ] `GET <url>` returns `200`, `Content-Type: application/json`, body starts with `{`.
- [ ] `items` is an array; each item has a non-empty `title`.
- [ ] Every item has a **stable, unique** `id` (re-fetching returns the same `id` for the
      same content).
- [ ] `title` (+ a `\n\n<url>` suffix when `url` is present) is ≤ 500 chars.
- [ ] `date_published` is valid ISO-8601 when present; newest item is resolvable.
- [ ] Empty state returns a valid feed with `"items": []` (not a 404 / not an error).
- [ ] Re-fetching with no new content returns the **same** items with the **same** ids
      (so the consumer correctly posts nothing).

## Minimal reference (illustrative — any language/host is fine)

```js
// Cloudflare Worker / any fetch handler
export default {
  async fetch() {
    const items = await getItems(); // your source; newest-first
    return Response.json({
      version: "https://jsonfeed.org/version/1.1",
      title: "Threads cron source",
      items: items.map((it) => ({
        id: it.stableId,                         // <- stable & unique
        title: it.text.slice(0, 440),            // <- the post body
        url: it.permalink,                       // optional
        date_published: it.publishedAtISO,       // optional but recommended
      })),
    });
  },
};
```

## Optional: RSS/Atom instead of JSON Feed

The consumer also accepts RSS/Atom. It reads `<title>`, `<link>` (RSS text content or Atom
`href`), `<guid>` / `<id>` (de-dup key), and `<pubDate>` / `<updated>` (sort). Same
semantics. JSON Feed is easier to produce correctly, so prefer it unless you already have an
RSS pipeline.
