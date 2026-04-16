# c

Personal CLI toolbox. Starts with an AWS subcommand group; more to come.

## Install

```sh
pip3 install --user -e .
```

Make sure `~/.local/bin` is on your `$PATH`:

```sh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc && source ~/.zshrc
```

Verify:

```sh
c --help
c aws --help
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

Provisions a static website behind two CloudFront distributions. Every
resource is created with a direct `aws` cli call; each step first checks
whether the target resource already exists and skips if so — re-running the
command resumes from wherever it left off.

Resources, in order:

1. `s3://DOMAIN` — private, `BucketOwnerEnforced`, full public-access block.
2. `s3://www.DOMAIN` — S3 website redirect to `https://DOMAIN`.
3. CloudFront Origin Access Control (`DOMAIN-oac`).
4. CloudFront distribution for `DOMAIN` (HTTPS, HTTP→HTTPS redirect, origin = root bucket via OAC).
5. Root bucket policy: `cloudfront.amazonaws.com` gets `s3:GetObject` scoped by `AWS:SourceArn` of the root distribution.
6. CloudFront distribution for `www.DOMAIN` (origin = www bucket's S3 website endpoint).
7. Route53 A + AAAA alias records (UPSERT) for `DOMAIN` and `www.DOMAIN`.

Pre-flight (stops before touching anything on failure):

1. `aws` is installed and your profile has working credentials.
2. A **public** Route53 hosted zone exists for `DOMAIN`.
3. An **ISSUED** ACM certificate in `us-east-1` covers both `DOMAIN` and
   `www.DOMAIN` (wildcard `*.DOMAIN` + apex counts).
4. Neither bucket name is owned by a different AWS account.

```sh
c aws --profile work static-site example.com
```

Re-running is idempotent: existing resources are detected and left alone.

## Layout

```
c/
├── pyproject.toml
└── c/
    ├── cli.py            # `c`
    └── aws/
        ├── cli.py        # `c aws`
        ├── runner.py     # subprocess wrapper around the aws cli
        ├── check.py      # `c aws check`
        └── static_site.py# `c aws static-site` (imperative aws cli calls, check-then-create)
```

Adding a new tool group (e.g. `gh`, `docker`): create `c/<group>/cli.py` with a
Click group, import it in `c/cli.py`, and register it with `main.add_command(...)`.
