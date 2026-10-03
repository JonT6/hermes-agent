# Deploying the deals plugin (SEC-14)

Twelve tools in the `deals` toolset. `deals_list`, `deals_show` and `deals_status` only read.
`deals_approve`, `deals_skip`, `deals_edit`, `deals_media`, `deals_pause`, `deals_resume`,
`deals_post_now`, `deals_get_posts` and `deals_from_link` refuse unless the turn came from `allowed_user_id` on
Telegram. Every tool runs the worker's CLI with `--json` (contract: the secret-deals repo's
`docs/cawl-contract.md`). There is no webhook and no wake. Cawl answers in the Candidates topic,
and a reply to a card carries the card's text.

- ⚠️ **`deals_post_now` publishes to the public channel at once** (`post-now`, SEC-46). The worker
  re-checks first and may hold instead, but a post that goes out cannot be taken back by any tool.
- **`deals_get_posts` sends Jonathan the two ready-to-copy posts itself** (`posts`, SEC-48). The
  post text is not in the tool result, on purpose. Cawl never re-types, summarises or rewrites
  post copy: it calls the tool and reports only whether the posts were sent.
- **`deals_from_link` drafts a candidate from an AliExpress link Jonathan pastes** (`from-link`, SEC-47). It never
  posts: it sends a card to Candidates, and he approves, posts now or edits from it. It spends a paid model call
  and takes a minute or more, so the tool waits 30 s and then answers "still drafting" **without killing the
  worker**. The plugin gives the worker its own session and temp-file output, so it finishes even if the gateway
  restarts, and a thread logs how it ended (`finished after the timeout`, in the gateway log). The tool passes
  `--reply-deadline` (now + 30 s − 2 s, so a failure at the boundary is said twice, never lost): a worker that fails after it posts "couldn't draft <link>: <reason>" to ✅ Approvals
  itself, once, and a failure before it is only the answer Cawl shows. A second call for a product that is still
  drafting is refused by the worker (`already_drafting`: "Already drafting this one") and starts nothing. A run killed
  outright posts nothing: the gateway log has it.

The candidate cards' **approve / approve-both / skip buttons** (SEC-71, SEC-102, `sd:` callbacks in the Telegram adapter,
not this plugin) read the same `plugins.entries.deals.settings`: the same worker, and the same
`allowed_user_id` as the one owner. A tap from anyone else is refused.

## Before you start

- **The worker must have SEC-43 live on the Mini.** The tools pass `--json`. A worker without it
  rejects the flag, and every tool then answers "The worker gave no answer".
  Check from the worker dir: `.venv/bin/secret-deals status --json` prints one JSON line.
- **`deals_post_now` needs SEC-46 and `deals_get_posts` needs SEC-48 on the worker.** Without
  them argparse rejects `post-now` / `posts`, and the tool answers the same way. Check with
  `.venv/bin/secret-deals post-now --help` and `.venv/bin/secret-deals posts --help`.
- **SEC-102 (`deals_approve`'s optional `placement`, and the ✅ Telegram + Facebook button) needs the worker's
  `approve --placement tg|both`.** Without it argparse rejects the flag and the tool or button answers "The
  worker gave no answer". Check with `.venv/bin/secret-deals approve --help | grep -- --placement`. The
  button waits at most 30 s for the worker (the Telegram callback answer comes after it), so a `both`
  approval slower than that is reported as "unknown": check with `deals_show` before retrying.
- **SEC-44 (an approval bound to its card) needs the worker's `--card` first.** `deals_approve`,
  `deals_post_now` and the ✅ Telegram / ✅ Telegram + Facebook buttons always pass `--card=<message id>`; a tool call whose turn
  answers no card is refused and never reaches the worker (Jonathan, 2026-09-28: every approval
  through Cawl is tied to a card). A worker without SEC-44 rejects the flag (no JSON, "The worker gave no answer"). Check
  with `.venv/bin/secret-deals approve --help | grep -- --card`. It also needs this checkout's
  gateway (`HERMES_SESSION_REPLY_TO_MESSAGE_ID`, bound in `gateway/run_turn.py`) **and** this
  plugin copy, deployed together. A SEC-44 plugin on an older gateway refuses every approval
  ("The gateway does not say which message this answers"). An older user-dir
  plugin on a SEC-44 gateway passes no card from a text reply, so that route stays unbound.
- **`deals_from_link` needs the worker's `from-link` (SEC-47), deployed with SEC-112, AND its `--reply-deadline` and
  `already_drafting` (SEC-47 part 3, 2026-10-03). Deploy the worker FIRST.** Without them argparse rejects the command
  or the flag, and every call answers "The worker gave no answer". Check with
  `.venv/bin/secret-deals from-link --help | grep -- --reply-deadline`. The plain `from-link` has been on the Mini since
  2026-10-02 (`623089e`); the flag has not.
- **The tools run `<venv>/bin/secret-deals`**, the sibling of `worker_python`, with `worker_dir`
  as the cwd and a bare environment (PATH, HOME, LANG, LC_ALL, TMPDIR, TZ). This is the same way
  the launchd jobs run. `python -m secret_deals` does not work, because the package has no `__main__`.

## 1. Put the plugin where Hermes scans

Pick one.

- **User dir (recommended).** Copy this directory to `~/.hermes/plugins/deals/` from a reviewed
  commit, without moving the running checkout:
  ```sh
  git -C ~/.hermes/hermes-agent fetch fork sec-14-deals-plugin
  mkdir -p ~/.hermes/plugins
  git -C ~/.hermes/hermes-agent archive <sha> plugins/deals | tar -x -C ~/.hermes/plugins --strip-components=1
  ```
- **Bundled.** Take it with the checkout, as `plugins/deals`, when the Mini next pulls a branch
  that contains it.

If both copies exist, the user-dir copy wins, because later sources override bundled ones on the
same key. Only the user-dir copy would then run.

## 2. `~/.hermes/config.yaml`

**Add** to the existing lists. Do not replace them. This is every key the plugin and the card
buttons need; nothing else in `config.yaml` changes.

```yaml
plugins:
  enabled:
    - deals            # append; a plugin outside plugins.enabled never loads, bundled or not
  entries:
    deals:
      settings:
        worker_python: /Users/jonathantoledano/Projects/secret-deals/secret-deals/.venv/bin/python
        worker_dir: /Users/jonathantoledano/Projects/secret-deals/secret-deals
        allowed_user_id: 77770367

platform_toolsets:
  telegram: [a2a, bfl, browser, ..., deals]   # the existing telegram list, plus deals
```

- `plugins.enabled` gets `deals`. The tools load only then.
- `plugins.entries.deals.settings` holds all three settings below. The **card buttons read them
  even when `deals` is not in `plugins.enabled`**, so this block is needed for the buttons alone.
- `platform_toolsets.telegram` gets `deals`, so a Telegram session is offered the tools.
- The buttons' first gate is the adapter's existing callback allowlist: the same Telegram
  authorisation that already lets Jonathan talk to Cawl. It needs no new key.

⚠️ **Once enabled, `deals` is on for EVERY platform by default, not only Telegram.** That is
`hermes_cli/tools_config.py` `_enabled_plugin_toolsets`: a plugin toolset is on unless
`known_plugin_toolsets.<platform>` names it and that platform's list leaves it out. On cli, cron,
webhook and a2a turns the reads work and every action is refused. The gate refuses because those
turns bind no Telegram user (cron binds `""`, a webhook binds `webhook:<route>`). To hide the tools
from a platform as well, add `deals` to `known_plugin_toolsets.<platform>` and leave it out of
`platform_toolsets.<platform>`.

## 3. Restart the gateway

Plugins load at discovery and toolsets freeze at session start:

```sh
supervisorctl -c ~/.hermes/supervisor/supervisord.conf restart hermes-gateway
```

A session that was already open keeps its old tool list. If Cawl says it has no deals tools in
the Candidates topic, start a new session there (`/new`).

## 4. Check it

- `hermes plugins list` shows `deals` enabled, with no error.
- In the Candidates topic, Jonathan asks for the deals status. Cawl answers with paused/running,
  today's spend and the pending count.
- An action from any other account, or from a cron job, is refused with "Refused to …: only
  Jonathan can do this". Nothing reaches the worker.

## Settings

| key | what |
|---|---|
| `worker_python` | the worker venv's python; the tools run its sibling `secret-deals` script |
| `worker_dir` | the worker repo, the directory holding `pyproject.toml`; the cwd for every call |
| `allowed_user_id` | Jonathan's Telegram user id, the one owner of every deals action: the mutating tools **and** the card buttons (SEC-71). If it is unset, every tool action and every button tap is refused |

Timeouts are 30 s; 180 s for `deals_edit` (one or two paid model calls plus a new card); 30 s for `deals_from_link`,
which is a wait, not a limit: after it the worker is left running and the tool says it is still drafting; 300 s for
`deals_post_now` and `deals_get_posts` (a live AliExpress re-check or link call, which may sit in
the client's own rate-limit retries, then Telegram sends with media uploads). When an action times
out, the tool says the outcome is unknown and to check with `deals_show`. For `deals_post_now`,
look at the channel too: the post may have gone out.
