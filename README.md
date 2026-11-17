# Babylist Shower Agent

**An event-driven AI agent that plans a baby shower across chat, email, and Google Calendar.** Start a plan in chat, close the tab, and let the conversation continue over email. A guest can reply the next morning and wake the agent, which picks up with the latest availability, the original email thread, and the decisions the organizer has already made.

I built the orchestration around that handoff. A SQLite inbox persists incoming work, a background worker resumes the right event, and Claude handles the parts that require judgment, like interpreting a vague RSVP or deciding whether to ask a follow-up. Python handles calendar updates, venue checks, and the rules around what the agent can send or change. The organizer can see what happened in the chat, the availability grid, and each guest’s email thread.

## From a guest reply to an agent run

1. **Send a threaded invitation.** The agent emails guests through Gmail with a tokenized `Reply-To` that identifies the event and participant. It tracks message IDs and references for threaded follow-ups and shows each participant’s transcript in the app.
2. **Put the reply in the inbox.** SendGrid forwards the reply to a FastAPI webhook. The inbound handler checks the token and sender, trims quoted text, and stores the reply in SQLite. A unique message ID keeps duplicate webhook deliveries from being processed twice.
3. **Wake the worker.** A queue coalesces pending wake-ups for the same event. The worker loads saved state and consumes pending inputs in order. Organizer chat messages and UI actions also use the inbox. A separate timer thread checks for due reminders and pending work, so the model can stay idle between runs.
4. **Act on the new information.** Claude gets the current plan and what changed, then runs a bounded tool loop. A reply like “Saturday works unless I’m on call” can trigger a clarifying email. If a key guest drops out after booking, the agent asks the organizer whether to reschedule. Approved changes update Google Calendar and notify participants.

## What the demo shows

- **Multi-party scheduling.** Collect RSVPs or negotiate a date window, with a restaurant’s availability shown alongside the guests’.
- **Venue and shopping workflows.** Explore San Francisco restaurants and picnic sites, check availability, and build an editable shopping list from local Kroger products. Venue booking is handled by the organizer, and grocery checkout is simulated.
- **An inbox you can test in minutes.** Claude plays guests and the restaurant, with replies arriving about 10 seconds apart by default. The simulated replies enter the same inbound parser and inbox as production replies, so the demo exercises the asynchronous workflow. Outbound demo emails go to plus-addresses in your own Gmail.

## Run it

```bash
cp .env.example .env            # add ANTHROPIC_API_KEY, GOOGLE_CLIENT_ID/SECRET, KROGER_CLIENT_ID/SECRET
uv sync                         # creates .venv from pyproject.toml / uv.lock
uv run uvicorn app:app --port 8000
```

Open **http://localhost:8000**. Use `localhost`, not `127.0.0.1`, because Google
redirects back to exactly the address you registered.

## One-time Google setup (you, not your users)

Your users only ever see Google's standard "Event Organizer wants to… Allow"
screen. For Google to show that screen, the app has to be registered once:

1. In https://console.cloud.google.com, open your project.
2. Go to **APIs & Services → Library** and enable **Gmail API**, **Google Calendar API** and **People API**
   (People API powers guest autocomplete from contacts).
3. Go to **APIs & Services → OAuth consent screen** (now **Google Auth Platform**) and click **Get started**:
   app name *Event Organizer*, your email for support and contact, Audience **External**.
4. Go to **Audience → Test users** and add your Gmail address, plus anyone else who will try it (up to 100).
5. Go to **Clients → Create client**, choose **Web application**, and add this
   **Authorized redirect URI**: `http://localhost:8000/auth/callback`. Create it,
   then copy the **Client ID** and **Client secret** into `.env`.

While the app is in Testing, only test users can connect. Opening it to anyone
requires Google's review of the app. The permissions requested are send email,
create calendar events, see free/busy times and, optionally, read contacts for
guest autocomplete. None of them read the inbox, so the review is lighter than for
apps that do.

**Guest suggestions and "most emailed".** Google's People API has no
"frequently emailed" data. Autocomplete searches saved contacts and "Other
contacts" (people Google saved automatically because you've emailed them). The
suggestion chips are your starred contacts, then recently updated ones. True
"who you email most" would need reading Gmail message headers, a *restricted*
permission that requires Google's yearly security assessment, so it's left out.

## The live demo

1. Open http://localhost:8000 (a mock registry dashboard) and click **Get help planning your
   baby shower**, then **Try it now**. The chat starts right away; no sign-in yet. Its intro
   shows two nearby restaurants and a park as cards (from your IP address; outside SF
   it shows SF venues). Clicking a card opens it in Google Maps. The right column stays
   hidden until the agent starts.
2. Answer the questions by clicking, or type (e.g. *"My sister Maria (maria@x.com) is
   hosting at her place, sometime in spring"*): **who's hosting** (you, or someone else's
   name and email; another host is invited as a key person), **at home or out**, **a
   specific date or a window** (1–88, 89–111 or 112–149 days out), and time of day.
3. **Connect Google** when asked, then come back to the chat.
4. Click **Add sample guests**. This turns on demo mode and stars Ana as a key
   person. Invitations really go out from your Gmail, but to `you+ana@…`,
   `you+ben@…` and `you+chloe@…`, so they land in your own inbox.
5. Depending on your answers:
   - **Out:** pick a venue from the cards in the chat. If you skip it, the agent brings the
     cards back when things go quiet (about 40 seconds in demo mode, up to 3 times) and when a
     date is proposed or booked, since the venue should be lined up alongside the date. Its status shows at the top of
     the Availability tab.
     - For a **restaurant**, the agent emails it. The email goes to a plus-address
       of yours; the card shows the real address it would go to. A simulated events
       manager replies, and the restaurant becomes a row in the availability grid.
     - For a **picnic area**, the app checks the date with SF Rec & Park. If it's
       taken, it offers the nearest free ones; otherwise it gives you the booking
       link and asks whether you booked it.
   - **At home** or a **picnic:** say yes to the shopping list (in the chat, or the button
     in the Shopping list tab, which gets a red dot) and watch it fill in (about 10
     seconds). Edit it there (swap products, quantities, remove, add an item) or ask the
     agent ("add some chocolate milk"). Then pick a delivery window on the morning of the
     shower. The app adds a Google Calendar reminder and wakes the workflow before the
     event to ask for payment confirmation. **Skip ahead to grocery ordering** jumps
     to that step in demo mode. Payment and order placement are simulated.
   - **A window:** the guests reply, Ben vaguely at first, and the agent recommends
     a date in the chat. Click **Book** to add it to your calendar. The agent
     confirms with everyone and the restaurant.
   - **A specific date:** guests RSVP. The date goes on your calendar right away at
     home, or once the venue is settled.
6. **Someone drops out (you trigger it):** once a date is booked, the top of the right
   column has a button per simulated guest, e.g. **Ben can't make it** or **Ana can't make
   it (key)**. Simulated guests never drop out on their own. For a regular guest the agent
   records it, replies kindly and tells you. For a key person (starred) it asks whether to
   reschedule; approve, then pick a new date or window. Everyone is told, and the calendar
   event moves. Star Ben in the guest list if you want his dropout to trigger the reschedule.
7. **You move it:** type *"let's move the date"*. Off-topic questions get a short,
   polite redirect.
8. **Reset demo** (top right) deletes the calendar events it created, removes the
   app's access to your Google account, and wipes your data.

In the chat, each round of emails shows as a small stack of envelope cards (sent in
purple, replies in green); hover to spread them out. Click a name to open that person in
the **Invitees** tab: their status (green confirmed, yellow waiting, red can't make it),
what they said, and the full email thread.

## Runtime and scheduling

Each model run has a 20-step limit. SQLite stores event state, chat, and pending inbox items. The application runs one queue worker thread and a separate timer thread that checks for due work every minute. Typed setup messages go to Claude, while setup buttons go directly through application code.

## How replies reach the agent in production

Every invitation has a `Reply-To` of `reply+<token>@REPLY_DOMAIN`. SendGrid
Inbound Parse receives replies to that domain and posts them to
`/inbound/sendgrid`. Demo guests use exactly the same format and code path. To go
live:

1. Pick a subdomain you own, e.g. `replies.yourdomain.com`, and add an MX record
   pointing it at `mx.sendgrid.net` (priority 10).
2. Deploy the app somewhere public. For a quick test, `ngrok http 8000` works.
3. In `.env`, set `REPLY_DOMAIN` and a long random `INBOUND_SECRET`.
4. In SendGrid, go to **Settings → Inbound Parse** and add the subdomain with the
   URL `https://<your-app>/inbound/sendgrid?key=<INBOUND_SECRET>`. Leave "raw" unchecked.

## Guardrails enforced in code

- The agent can email only the event's guests and the current restaurant. Their emails are treated as data, never as instructions.
- Restaurant emails always go to a plus-address of your own Gmail for now, never to the real restaurant.
- No follow-up until 48h after the last unanswered email, at most 2 reminders per guest, and at most 2 emails per person per agent turn.
- A date can be proposed only after everyone (including the restaurant) has answered and it works for all of them. You always click Book.
- Only you can move the date: your own message, or approving the agent's question, and then you pick the new date.
- A reply counts only if it comes from the invited address, and the webhook requires a secret.
- Typed messages are capped at 500 characters and 40 per event.
- Users' Google tokens are encrypted at rest.

## Code

| File | What it does |
|---|---|
| `agent.py` | The agent: instructions, tools, guardrails and loop; also reads typed setup messages |
| `chat.py` | The chat: setup steps, widgets, email cards |
| `worker.py` | Wakes the agent when something happens; calendar, venue checks, moving the date |
| `venues.py` | Saved SF restaurants and picnic areas; live Resy and SF Rec & Park checks |
| `shopping.py` | The shopping list: Claude plans it, Kroger finds the products |
| `personas.py` | Simulated guests and restaurant |
| `app.py`, `templates/` | The web app: one two-column page per shower |
| `google_api.py` | Google sign-in, sending Gmail, Calendar, Contacts |
| `inbound.py` | Replies (SendGrid Inbound Parse format) |
| `models.py`, `db.py` | Event state and SQLite storage (`instance/phase2.db`) |
| `data/`, `static/picnic/` | The saved venue lists and picnic photos |
| `scripts/` | Rebuild the venue lists (see below) |

### Refreshing the venue lists

They're built offline from the sibling projects, then saved here so the page is instant:

```bash
uv run python scripts/build_restaurants.py --live   # Resy, via ../sfparkandrec (add --claude to search for missing emails)
cd ../actualsfpark && uv run python ../babylist_shower_agent/scripts/build_picnic.py
```

Check the restaurant emails by eye after a rebuild: scraping sometimes finds the wrong
address.

## Tests

```bash
uv run pytest -q
```
