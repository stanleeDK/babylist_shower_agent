# Phase 2 spec (agreed)

Source requirements: `requireemtns for phase 2.md`. This file records the decisions made while reviewing them.

## Page and layout
- One page per event, built for a laptop screen. Columns stack below ~900 px.
- **Left column:** the chat, with the input box pinned at the bottom.
- **Right column:** tabs that switch automatically to whatever just changed: Venues · Availability · Shopping list · Emails · Agent activity.
- **Top bar:** Demo toggle, a "⭐ X can't make it" button for each key person, Reset demo.
- **Header:** raffle copy with a placeholder Terms link. Nothing is collected.
- The database starts fresh. The home page stays a simple list of events.

## Chat
- A real LLM runs the conversation: Opus 5.5 at low effort, one agent per event.
- Widget clicks are handled by code, so they're instant. The agent is woken for typed messages, incoming emails, and moments where it has something to report.
- Off-topic messages get a brief redirect back to the event. Typed messages have a length cap and a per-session limit.
- If the API fails, the chat falls back to the scripted path.

## Setup steps
1. Honoree.
2. At home or out. The Venues tab shows a preview, but you can't pick yet.
3. A single date, or one of three windows: days 1–88, 89–111, 112–149. The 112–149 chip gets a "Most popular" badge, with the line "Most people book their shower 111–117 days out." The agent does not prefer those dates.
4. Morning / afternoon / evening. For venues these mean 11:00 / 13:00 / 18:00.
5. Connect Gmail. You can chat anonymously up to this point, and come back to the chat after the Google sign-in.
6. Guests, with contact autocomplete.
   - Each guest gets a ⭐ key-person toggle. The honoree is always key.
   - In demo mode, "Add sample guests" fills in the three personas and Ana is starred.

## Flows
- **Specific date:** RSVP invitations. In demo mode every guest says yes.
- **Window:** the current date negotiation.
- **Out of home on a specific date:** pick the venue right away.
- **Out of home on a window:** pick the venue while invitations go out.

### Restaurant
- The agent emails the restaurant. The email actually goes to a plus-address on the organizer's Gmail, and the card shows "would send to …".
- One live Resy check confirms real open times.
- The simulated restaurant replies:
  - On a window, it is open on the date the guests settle on and booked on a couple of others.
  - It appears as a row in the availability grid.
- Once the date is set, the agent confirms the final headcount and the restaurant replies yes.

### Picnic table
- A saved SF list. One live availability check on the chosen date; if that table is taken, suggest the nearest free one.
- Booking links out to SF Rec & Park, and the agent asks "Did you book it?"
- Picnic tables are not a row in the availability grid.
- For dates more than 90 days out, say "Booking opens on …".

### Shopping list
- When it's offered: when invitations go out (at home), or once a picnic table is chosen. Never for a restaurant.
- 10 items, planned for the number invited + 1.
- Store: the nearest Kroger-family store, falling back to 94118 (in SF, FoodsCo Folsom St).
- Product cards show photos and link to kroger.com.
- You can remove items, swap among alternatives, and change quantities. You can't add items.
- A pickup/delivery toggle marks items not available that way.
- "Shop this list at Kroger →" opens kroger.com. There is no real cart.

### Changes
- **"Change the date":** asks for a specific new date or a new window.
  - Guests and the restaurant are re-contacted.
  - For a picnic table: a cancel reminder plus the 3 nearest free tables.
  - The calendar event is moved.
- **Key person can't make it:** the agent asks "reschedule?" and needs the organizer's approval.

### Stopping short
- The agent never books a restaurant, table, or groceries.
- It still creates the Google Calendar event.

## Speed and data
- Venue data:
  - Restaurants: a saved SF snapshot built by an offline script, with photos and checked emails.
  - Picnic sites: a saved SF snapshot with photos copied from actualsfpark.
  - All venues are in SF. Visitors elsewhere see a note saying so.
- Simulated replies arrive ~2–4 s apart and are written by Haiku 5.5.
- Shopping list: Sonnet 5.5 (low effort) plans it, Haiku 5.5 picks the products. Target: under 10 s.
- The Agent activity tab logs thinking, decisions, and emails, plus venue, Resy, and Kroger calls with timings.
