# MAD Fitness: studio management prototype

A working prototype of personal-training studio software, built to compare with PT Minder. It covers trainer schedules, 1:1 and small-group sessions, recurring classes with waitlists, packages and memberships, a late-cancel policy, Stripe test-mode payments, a mobile client booking page, and a trainer payroll report.

**Stack:** Python 3.11+ and Django 5.2 LTS. It uses SQLite locally and Postgres in production. Pages are rendered on the server with plain HTML and CSS, so there's no JavaScript build step. Tests use pytest.

## Start it

You need macOS or Linux with Python 3.11 or newer. On Windows, use WSL.

```bash
git clone https://github.com/swallsten/mad_fit.git
cd mad_fit
./run.sh
```

The first run takes about a minute. It creates `.venv`, installs the dependencies, creates `.env` from `.env.example`, builds the database and loads the demo data. Then open **http://localhost:8000**.

`./run.sh --reset` wipes the data and re-seeds it. The demo data is dated relative to today, so re-seeding keeps it current.

To run the tests:

```bash
.venv/bin/pytest
```

## Online demo (no install)

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/swallsten/mad_fit)

The repo includes a [`render.yaml`](render.yaml) that runs a free public demo on [Render](https://render.com):

1. Create a free Render account. Signing up with GitHub is easiest.
2. Click the **Deploy to Render** button above. If Render asks, let it access the `mad_fit` repo.
3. Click **Deploy Blueprint**. The first build takes about 3–5 minutes. When it finishes, Render shows the site's address, something like `https://mad-fitness-demo.onrender.com`.

Send that link to reviewers. The login page shows one-click buttons ("Log in as Jordan", "Log in as Emily", and so on), so nobody has to type an email or password.

How the free demo behaves:
- **It sleeps when idle.** After about 15 minutes without visitors it goes to sleep. The next visitor waits about a minute while it wakes up.
- **It resets itself.** Each time it wakes, it reloads fresh demo data dated from that day. Anything a reviewer changed is wiped, which keeps the demo clean.
- **Online payments are off.** The Buy buttons are disabled. Adding a Stripe test key in Render's *Environment* tab turns them on.
- **Updates are automatic.** Pushing changes to `main` redeploys the site.

`DEMO_MODE=1` (set in `render.yaml`) is what shows the one-click logins and the demo password. Never turn it on for a site with real client data.

## Logging in

Every demo account uses the password **`demo1234`**.

| Role | Login | What to look at |
|---|---|---|
| Admin (owner) | `owner@studio.test` | All 7 calendars side by side (Day / Week), Clients, Trainer report, Classes, Settings |
| Trainer | `alex@studio.test` | Own week, own clients, availability and time off, marking attendance |
| Other trainers | `maya@`, `chris@`, `sam@`, `priya@`, `dev@`, `leah@studio.test` | Maya has a day off in 3 days; Sam runs the small-group sessions |
| Client | `emily.carter@example.com` | A 60-min pack plus an Unlimited Classes membership; **#1 on the waitlist** for the next full Bootcamp |
| Client, expired package | `dana.whitfield@example.com` | 3 credits that expired 20 days ago; booking is refused |
| Client, failed payment | `marcus.bell@example.com` | Flagged: red banner, new bookings blocked, existing bookings kept |
| Client, in the full class | `olivia.brooks@example.com` | Booked in that Bootcamp. If she cancels, Emily is promoted automatically |

To see waitlist promotion: log in as Olivia, go to Classes, and cancel Bootcamp. Then log in as Emily, who is now booked and has a notification queued in the outbox. (Emily's membership covers the class, so no credit is used.) The owner can also open `/django-admin/` with the same login to edit any record directly, including products.

## How it maps to PT Minder

| PT Minder | Here |
|---|---|
| Trainers, availability, time off | **Availability** page: weekly hours plus time-off blocks |
| Sessions (1:1, semi-private) | 30- or 60-min **1:1 sessions** booked into open slots; **small-group sessions** for 2–4 clients |
| Classes, waitlist | Recurring **classes** (Classes → schedule upcoming dates), capacity, automatic waitlist promotion |
| Packages, memberships, drop-ins, intro offers | **Products**. Credits are typed (30-min, 60-min, small-group, class), so a 30-min pack can't pay for a 60-min session |
| Cancellation policy | **Settings → late-cancel hours**. Staff can waive a late cancel when cancelling |
| Client booking page | `/me/` on a phone: balance, upcoming, book, classes, buy, billing |
| Payroll report | **Trainer report** for any date range, with CSV download |

## Rules the code enforces (and tests)

- **Deduction.** Booking takes 1 credit from the package that expires soonest; packages with no expiry are used last. An unlimited membership is used before any package. The package must still be valid on the *session date*. Every change is written to a credit ledger that reconciles with the balance.
- **Expired packages** can't be used. Clients and staff see their expired credits separately.
- **No double-booking.** A trainer can't be booked at the same time as another session or class, during time off, or outside posted hours. Staff can override posted hours, but never time off or an overlap. A client can't hold two overlapping bookings. Booking runs inside a locked database transaction, and a test fires four simultaneous requests at one slot to show only one wins.
- **Late cancel.** Inside the window (default 24h) the booking becomes "Late cancel" and the credit is kept. Outside it, the credit is refunded. A no-show keeps the credit. If the studio cancels a class, everyone is refunded.
- **Waitlist.** Joining doesn't use a credit. When a seat opens, the first person in line is booked and charged. Anyone who no longer has credits, is flagged, or is now double-booked is skipped. There's no automatic promotion within 2h of the start (configurable).
- **Failed payment.** `invoice.payment_failed` flags the client and marks the membership past due. New bookings are blocked; existing ones stay. The next `invoice.paid` clears the flag, and the admin can also clear it by hand.
- **Packs don't reset monthly.** The "4 Sessions / Month" membership adds 4 credits on each paid invoice, and unused ones roll over (they expire after 90 days).

## Connecting Stripe (test mode)

The app **refuses live keys**: anything not starting with `sk_test_` or `rk_test_` is rejected.

1. In the Stripe dashboard, switch to **Test mode** and copy the secret key from *Developers → API keys*.
2. Put it in `.env`:
   ```
   STRIPE_SECRET_KEY=sk_test_...
   ```
3. Turn on the customer portal once: in test mode, go to *Settings → Billing → Customer portal* and click **Save**. Without this, Stripe returns an error when a client clicks "Billing & card".
4. Restart `./run.sh`. The **Buy** buttons are now live. Stripe products and prices are created automatically the first time each product is bought. Use test card `4242 4242 4242 4242`, with any future date and any CVC.

Purchases are fulfilled when the client lands on the success page, so **packages and the first month of a membership work without webhooks**. Renewals, failed payments and cancellations need webhooks. Locally, install the [Stripe CLI](https://stripe.com/docs/stripe-cli) and run:

```bash
stripe listen --forward-to localhost:8000/stripe/webhook/
```

Put the `whsec_...` value it prints into `.env` as `STRIPE_WEBHOOK_SECRET`.

To try a failed payment end to end with webhooks running:

1. Log in as a client and subscribe with `4242...`.
2. Open **Billing & card** and replace the card with `4000 0000 0000 0341`. Stripe accepts this card when it's saved but declines every charge.
3. In the Stripe dashboard, create an invoice for that customer and click *Charge customer*.

The decline sends `invoice.payment_failed`. The client is flagged and blocked from booking until a payment succeeds or the admin clears the flag. (`stripe trigger` won't flag anyone because it invents a customer this app doesn't know about.)

## Project layout

```
studio/models.py              data model (trainers, clients, products, packages, events, bookings, ledger)
studio/services/booking.py    booking, cancel, waitlist, availability, double-booking locks
studio/services/credits.py    balances, deduction order, refunds
studio/services/payments.py   Stripe Checkout, portal, webhook handlers (idempotent)
studio/services/reports.py    trainer payroll report
studio/views/                 client.py (booking page), staff.py (trainer+admin), office.py (admin only)
studio/management/commands/seed.py   demo data
tests/                        pytest suite
```

## Designed for later (not built)

- **Email and SMS reminders.** Waitlist promotions already write to a `Notification` outbox table. Adding a sender (for example Postmark or Twilio) and a scheduled job is all that's missing.
- **Google Calendar sync.** Every booking is an `Event` with a trainer and start and end times, which maps directly to calendar events per trainer.
- **PT Minder import.** `ClientProfile.external_id` holds their client ID, and packages have an `import` source, so imported balances get a ledger entry like everything else.
- **Workout and nutrition plans.** These would be a new app attached to `ClientProfile`. Nothing in the booking code would need to change.
