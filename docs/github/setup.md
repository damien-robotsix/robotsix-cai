---
title: GitHub App Setup
nav_order: 1
parent: GitHub
---

# Setting up the cai GitHub App (one-time)

cai authenticates to GitHub as the `cai[bot]` identity via a GitHub App.
This file walks through the GitHub.com side. Once done, you only have
to install the App on each new repo (one click).

## 1. Register the App

Open https://github.com/settings/apps/new (or the org-level equivalent
if you want the App owned by an org).

| Field | Value |
|---|---|
| GitHub App name | `cai` (must be globally unique on GitHub; if taken, try `cai-<yourhandle>`) |
| Homepage URL | https://github.com/damien-robotsix/robotsix-cai |
| Webhook | **Uncheck "Active"** — cai is a client of the API, not a webhook receiver |
| Webhook URL | (leave blank) |
| Webhook secret | (leave blank) |

## 2. Set permissions

### Repository permissions

| Permission | Access | Why |
|---|---|---|
| Contents | Read & write | Push commits |
| Pull requests | Read & write | Open and review PRs |
| Issues | Read & write | Open issues + manage labels/milestones (sub-resources of issues) |
| Metadata | Read | Mandatory; auto-selected |

### Organization permissions

| Permission | Access | Why |
|---|---|---|
| Members | Read | Org-wide read scope |
| Projects | Read & write | Manage Projects (v2) |

Leave everything else unset.

## 3. Installation scope

Choose **Only on this account** unless you specifically want others to install it.

## 4. Create the App, then download the private key

Click "Create GitHub App". On the resulting App page:

- Note the **App ID** (top of the page, e.g. `1234567`).
- Scroll to "Private keys" → "Generate a private key". A `.pem` file downloads.

## 5. Drop credentials into the cai container

The `cai_home` volume mounts at `/home/cai`, so anything you put under
`/home/cai/.config/cai/` survives container rebuilds.

```bash
mkdir -p /home/cai/.config/cai
mv /path/to/cai.<date>.private-key.pem /home/cai/.config/cai/github-app.pem
chmod 600 /home/cai/.config/cai/github-app.pem
echo "APP_ID=1234567" > /home/cai/.config/cai/app.env
chmod 600 /home/cai/.config/cai/app.env
```

To put the `.pem` somewhere else, set `PRIVATE_KEY_PATH=/path/to/key.pem`
in `app.env`.

## 6. Install the App on a repo

On the App's public page (`https://github.com/apps/<your-app-name>`),
click "Install" → choose the repo(s).

## 7. Bootstrap a clone

Inside any clone of an installed repo:

```bash
cai-app-init           # auto-detects owner/repo from `origin`
# or
cai-app-init owner/repo
```

After that, `git push` and `from cai import CaiBot` both act as `cai[bot]`.
Other clones are unaffected.
