# iCloud Calendar Bridge for Hark

Lets your Hark see (and, when you ask, add to) your iCloud calendars. Hark cannot read iCloud directly, so this small robot does it: every 15 minutes it reads the calendars you choose and saves them as `calendar_snapshot.json` in your own private copy of this repository.

No coding needed. Your Hark can guide you through every step.

## Setup (about 15 minutes)

1. **GitHub account.** Sign up free at github.com with your normal email address.
2. **Your own copy.** On this page click **Use this template → Create a new repository**. Give it any name and choose **Private** (important: your appointments will be stored there).
3. **Apple app password.** Go to account.apple.com → Sign-In and Security → App-Specific Passwords → create one named "Hark". Copy it. (This is not your Apple ID password and can be revoked anytime.)
4. **Add two secrets.** In your new repository: Settings → Secrets and variables → Actions → **New repository secret**:
   - `ICLOUD_USERNAME` = your Apple ID email
   - `ICLOUD_APP_PASSWORD` = the app password from step 3
5. **Choose calendars.** Same page, tab **Variables** → **New repository variable**:
   - `ICLOUD_CALENDARS` = the exact calendar names, comma separated, e.g. `Brötchen,Bei dir?`
   The first name is your personal calendar, where Hark adds things for you.
6. **Start it.** Tab **Actions** → enable workflows → "Refresh private iCloud calendar snapshot" → **Run workflow**. After a minute `calendar_snapshot.json` fills with your events.
7. **Connect Hark.** In Hark: profile (top right) → Connected accounts → GitHub. Then tell Hark the name of your repository.

## Rules Hark follows

- Reads the snapshot; writes only via `calendar_commands.json` when you ask.
- Shared calendars are visible to others, so Hark asks before changing them.
- Recurring events and invitations with attendees are never changed or deleted.
- If GitHub pauses the workflow after a quiet period, re-enable it under Actions.
