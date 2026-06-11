# c

Personal CLI toolbox. Starts with an AWS subcommand group; more to come.

Works on Linux and macOS. Requires Python ≥ 3.10 and the `aws` cli on PATH.

## Install

The most reliable way on both platforms is [pipx](https://pipx.pypa.io)
(isolated venv, `c` on PATH, editable install keeps tracking this checkout):

```sh
pipx install -e .        # Linux: pip install --user pipx · macOS: brew install pipx
pipx ensurepath          # once, then restart the shell
```

### Linux (alternative: pip --user)

```sh
pip3 install --user -e .
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc
```

### macOS notes

- Homebrew's Python is [externally managed](https://peps.python.org/pep-0668/),
  so plain `pip3 install --user` is rejected — use pipx above (or a venv).
  The Xcode Command Line Tools' `pip3 install --user` works but drops the `c`
  script into `~/Library/Python/3.x/bin`, which is rarely on `$PATH`.
- AWS cli: `brew install awscli`. Python ≥ 3.10: `brew install python` if the
  system one is older.
- `c setup` writes Claude Desktop config to its macOS location
  (`~/Library/Application Support/Claude/`) automatically.

Verify:

```sh
c --help
c aws --help
c aws check     # confirms aws cli + credentials
```

## AWS

All `aws`-group commands accept `--profile` and `--region` (falling back to
`$AWS_PROFILE` / `$AWS_REGION` and finally the cli defaults).

### `c aws check`

Verifies `aws` is installed and your credentials work.

```sh
c --help
c aws --profile work check
```

### `c aws static-site DOMAIN`

Provisions a static website end to end — DNS, registrar delegation,
certificate, buckets, CloudFront — in one command. Every resource is created
with a direct `aws` cli call; each step first checks whether the target
resource already exists and skips if so — re-running the command resumes from
wherever it left off.

Resources, in order:

1. Route53 **public hosted zone** for `DOMAIN` (created if missing).
2. **GoDaddy nameserver delegation** → the zone's Route53 nameservers
   (best-effort: if the domain isn't in your GoDaddy account or no credentials
   are configured, the nameservers are printed for manual setup and
   provisioning continues; disable with `--no-godaddy`).
3. **ACM certificate** in `us-east-1` for `DOMAIN` + `*.DOMAIN`, DNS-validated
   automatically via Route53 (an existing cert covering `DOMAIN` and
   `www.DOMAIN` is reused).
4. `s3://DOMAIN` — private, `BucketOwnerEnforced`, full public-access block.
5. `s3://www.DOMAIN` — S3 website redirect to `https://DOMAIN`.
6. CloudFront Origin Access Control (`DOMAIN-oac`).
7. CloudFront distribution for `DOMAIN` (HTTPS, HTTP→HTTPS redirect, origin = root bucket via OAC).
8. Root bucket policy: `cloudfront.amazonaws.com` gets `s3:GetObject` scoped by `AWS:SourceArn` of the root distribution.
9. CloudFront distribution for `www.DOMAIN` (origin = www bucket's S3 website endpoint).
10. Route53 A + AAAA alias records (UPSERT) for `DOMAIN` and `www.DOMAIN`.

Pre-flight (stops before touching anything on failure):

1. `aws` is installed and your profile has working credentials.
2. Neither bucket name is owned by a different AWS account.

```sh
c godaddy configure                       # once: GoDaddy API key/secret
c aws --profile work static-site example.com
```

Re-running is idempotent: existing resources are detected and left alone.
If the nameservers were just delegated, ACM validation can take a while; the
command waits, and if it still times out you can simply re-run it later — the
pending certificate is reused.

### `c aws logs …`

Cross-function CloudWatch Logs for serverless apps. Each Lambda writes to its
own log group (`/aws/lambda/<fn>`); these commands treat a glob over log-group
names as the app/env selector and fan out across every match.

`PATTERN` is a shell glob (`*`, `?`, `[…]`). Without a leading `/` the default
prefix `/aws/lambda/` is prepended — so `myapp-prod-*` matches every Lambda
whose name starts with `myapp-prod-`. Provide a full path to target other
services (e.g. `/aws/apigateway/myapp-prod*`).

```sh
c aws logs list   myapp-prod-*                     # discover matching groups
c aws logs tail   myapp-prod-* -f ERROR            # live tail (≤ 10 groups)
c aws logs search myapp-prod-* -s 2h -f timeout    # history (≤ 50 groups)
c aws logs search myapp-prod-* -q 'stats count() by bin(5m)'
```

Under the hood: `describe-log-groups` for discovery, `start-live-tail` for
`tail`, `start-query` (CloudWatch Logs Insights) for `search`.

### `c aws zone ensure DOMAIN`

Creates a public Route53 hosted zone if missing and prints its nameservers.

### `c aws cert issue DOMAIN`

Requests a DNS-validated ACM cert in `us-east-1` for `DOMAIN` (+ `*.DOMAIN`
by default, extra SANs via `--san`), writes the validation CNAMEs into the
Route53 zone, and waits for it to be ISSUED. Reuses a matching existing cert.

## GoDaddy

Requires API credentials: run `c godaddy configure` (saved to
`~/.config/c/godaddy.json`) or set `GODADDY_API_KEY` / `GODADDY_API_SECRET`.
Generate Production keys at https://developer.godaddy.com/keys.

### `c godaddy set-ns DOMAIN`

Points a GoDaddy-registered domain's nameservers at its Route53 hosted zone
(or an explicit list via `-n`). No-op when they already match.

## Claude

### `c schedule-prompt WHEN PROMPT`

Blocks until WHEN, then opens Claude Code in a directory with PROMPT. Useful
for kicking off a long run overnight or after a rate-limit window resets.

```sh
c schedule-prompt 5h "Refactor the game loop" -C ~/dev/farming-game
c schedule-prompt 03:00 "Nightly deep-research pass" --model fable --ultracode
c schedule-prompt 2h30m "Fix the flaky tests" -p --permission-mode acceptEdits > run.log
c schedule-prompt 1h "fix CI" -- --allowed-tools "Bash,Edit,Read"   # extra claude flags after --
c schedule-prompt 4h "Ship the refactor" --model fable --ultracode --permission-mode auto  # autopilot
```

`WHEN` is a duration (`30s`, `5m`, `2h`, `1d`, compounds like `2h30m`), a
wall-clock time (`03:00` — today, or tomorrow if already past), an ISO 8601
timestamp, or `now`. Ctrl-C cancels the wait. When the time arrives the
process replaces itself with `claude` running in `--path` (default: the
current directory) — interactive by default, headless with `-p/--headless`.

Options pass straight through to Claude Code:

- `--model/-m` — alias (`fable`, `opus`, `sonnet`, `haiku`) or full name.
- `--effort/-e` — `low | medium | high | xhigh | max` (→ `claude --effort`).
- `--ultracode` — **not** an effort level; it's a per-session Claude Code
  setting (sends xhigh and orchestrates dynamic workflows), forwarded as
  `--settings '{"ultracode": true}'`. `--effort ultracode` is accepted as
  shorthand. Mutually exclusive with `--effort`.
- `--permission-mode` — for autopilot use `auto` (Claude Code's classifier
  approves/denies each tool call on its own — the right choice for scheduled
  runs nobody is watching). `acceptEdits` auto-approves file edits only;
  Bash/web still prompt, which stalls an unattended run unless you also pass
  `-- --allowed-tools …`. Think twice before `bypassPermissions` on a
  "change anything" prompt.
- `--dry-run` — print the schedule + final command and exit.

The wait recomputes remaining time from the wall clock every tick, so a laptop
that suspends mid-wait fires as soon as it wakes. On **macOS** the wait holds a
`caffeinate -i` assertion so the machine doesn't idle-sleep past the scheduled
time (the display may still sleep). On **Linux**, keep the machine awake
yourself if needed, e.g. `systemd-inhibit --what=sleep c schedule-prompt …`.

## MCP

`c` ships [Model Context Protocol](https://modelcontextprotocol.io) servers so
an MCP client (Claude Code / Claude Desktop) can drive the toolbox as tools.

### `c setup`

Registers the `c` MCP servers in the Claude config files — adds an entry to
each config's `mcpServers` so Claude launches `c mcp <server>` on startup.
Existing config is preserved and a `.bak` backup is written before any change.

```sh
c setup                       # claude-code (+ claude-desktop if installed)
c setup --target project      # write ./.mcp.json instead
c setup --dry-run             # show what would change, write nothing
```

Targets: `claude-code` (`~/.claude.json`), `claude-desktop` (the platform
Desktop config), `project` (`./.mcp.json`). After running, restart Claude.

### `c mcp [SERVER]`

Runs an MCP server over stdio — normally launched by the client, not by hand.
`c mcp --list` shows the available servers.

**`logs`** — AWS Lambda / CloudWatch Logs. Wraps `c aws logs` so an agent can
discover a serverless app's Lambda log groups and search/iterate their logs:

- `list_log_groups(pattern)` — discover Lambda log groups matching a glob.
- `search_logs(pattern, since, until, filter, query, limit)` — CloudWatch Logs
  Insights search across the matched groups, newest-first. Iterate further back
  by passing `until` = the oldest `timestamp` from the previous result.

## Layout

```
c/
├── pyproject.toml
└── c/
    ├── cli.py            # `c`
    ├── aws/
    │   ├── cli.py        # `c aws`
    │   ├── runner.py     # subprocess wrapper around the aws cli
    │   ├── check.py      # `c aws check`
    │   ├── zone.py       # `c aws zone ensure` + ensure_zone() core
    │   ├── cert.py       # `c aws cert issue` + ensure_certificate() core
    │   ├── logs.py       # `c aws logs {list,tail,search}` + search_log_groups() core
    │   └── static_site.py# `c aws static-site` (imperative aws cli calls, check-then-create)
    ├── claude/
    │   └── schedule_prompt.py # `c schedule-prompt` (block until a time, then exec claude)
    ├── godaddy/
    │   ├── cli.py        # `c godaddy`
    │   ├── api.py        # stdlib GoDaddy API client (sso-key auth)
    │   ├── configure.py  # `c godaddy configure` (save key/secret)
    │   └── set_ns.py     # `c godaddy set-ns` + ensure_godaddy_ns() core
    └── mcp/
        ├── cli.py        # `c mcp` (run a server) + `c setup` (register with Claude)
        ├── __init__.py   # SERVERS registry
        └── logs_server.py# `logs` MCP server — wraps `c aws logs`
```

Adding a new tool group (e.g. `gh`, `docker`): create `c/<group>/cli.py` with a
Click group, import it in `c/cli.py`, and register it with `main.add_command(...)`.
