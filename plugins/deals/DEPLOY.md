# Deploying the deals plugin (SEC-14)

Nine tools in the `deals` toolset. `deals_list`, `deals_show` and `deals_status` only read.
`deals_approve`, `deals_skip`, `deals_edit`, `deals_media`, `deals_pause` and `deals_resume` refuse
unless the turn came from `allowed_user_id` on Telegram. Every tool runs the worker's CLI with
`--json` (contract: the secret-deals repo's `docs/cawl-contract.md`). There is no webhook and no
wake. Cawl answers in the Candidates topic, and a reply to a card carries the card's text.

## Before you start

- **The worker must have SEC-43 live on the Mini.** The tools pass `--json`. A worker without it
  rejects the flag, and every tool then answers "The worker gave no answer".
  Check from the worker dir: `.venv/bin/secret-deals status --json` prints one JSON line.
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

**Add** to the existing lists. Do not replace them.

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
| `allowed_user_id` | Jonathan's Telegram user id. If it is unset, every action is refused |

Timeouts are 30 s, and 180 s for `deals_edit` (one or two paid model calls plus a new card). When
an action times out, the tool says the outcome is unknown and to check with `deals_show`.
