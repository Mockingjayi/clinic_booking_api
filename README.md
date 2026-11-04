# 🏥 Clinic Booking API

A booking API for a clinic, plus a simple web page that uses it. Patients can see available appointment slots, book one, and see a confirmation. Built for the Koya Academy Cohort 4 assessment, Task 1.

## 🔗 Live links

| What | Link |
|------|------|
| 🌐 Web page | https://clinic-booking-api-34ya.onrender.com/app |
| 📡 API base URL | https://clinic-booking-api-34ya.onrender.com |
| 💻 Code | https://github.com/Mockingjayi/clinic_booking_api |

Every API request needs the `X-API-Key` header. The web page adds it automatically.

## ✅ What is built

All five levels are done.

| Level | What it does | How it works |
|-------|--------------|--------------|
| 1 | List slots, book one, look up a booking | Plain endpoints backed by a PostgreSQL database, so bookings survive restarts |
| 2 | Only requests with the API key get in. Bad details are turned away | A check runs before every request and returns 401. Booking details are validated and answered with 400 or 404. Names and emails are trimmed and emails are saved in lower case |
| 3 | Nobody can book the same slot twice, even at the same moment. Bookings can be cancelled | A unique index in the database allows only one confirmed booking per slot. The code turns a clash into a 409. Cancelling sets the status to cancelled, which frees the slot |
| 4 | A retried request never creates a duplicate. People can page through their bookings | Each answer is saved under its Idempotency-Key together with a fingerprint of the request. Paging uses a bookmark called `next_cursor` |
| 5 | Spam protection | At most 5 booking attempts per email per minute. The 6th gets a 429 with a `Retry-After` header |

## 🧰 Tech stack

| Part | Choice |
|------|--------|
| API | Python with FastAPI |
| Database | PostgreSQL on Neon |
| Hosting | Render |
| Web page | One HTML file with plain CSS and JavaScript, served by the same app |

## 📡 Endpoints

| Method | Path | Level | What it does |
|--------|------|-------|--------------|
| POST | `/reset` | 1 | Deletes every booking and replaces all slots with the ones sent |
| GET | `/slots` | 1 | Lists every slot, earliest first. Optional `provider` filter |
| POST | `/bookings` | 1 to 5 | Books a slot. Accepts an optional `Idempotency-Key` header |
| GET | `/bookings?email=` | 4 | Lists one person's bookings, oldest first, with `limit` and `cursor` |
| GET | `/bookings/{id}` | 1 | Looks up one booking |
| DELETE | `/bookings/{id}` | 3 | Cancels a booking and frees the slot |
| GET | `/app` | | The web page. This is the only public address |

Every error looks like `{"error": "message"}`. All times are in UTC.

## 🧠 Design decisions

1. **The database enforces the hard rules.** A check in code can be beaten by two requests arriving at the same instant. So one confirmed booking per slot is a rule inside the database, and the spam limit counts attempts in the database under a lock for each email. Both stay correct no matter how many requests arrive together.
2. **Safe retries are remembered, not guessed.** The first answer to a request with an Idempotency-Key is saved, including its status code and booking id. A repeat with the same details gets the saved answer. The same key with different details gets a 422.
3. **A replay is not a new attempt.** A repeat that is answered from the saved answer does not use up one of the 5 attempts, because it creates nothing new.
4. **Failed attempts count.** A wrong slot, a missing field or a taken slot each use one attempt, as the spec requires. Emails are matched ignoring case and spaces, so capital letters cannot dodge the limit.

## 🚀 Run it on your own computer

You need Python 3 and a free PostgreSQL database, for example from Neon.

```
pip install -r requirements.txt
export DATABASE_URL="your database connection string"
export API_KEY="any long secret text you choose"
uvicorn main:app
```

To see the web page with sample data, run this in a second terminal and then open `http://localhost:8000/app`:

```
python3 seed.py
```

No secrets are stored in this repo. The database address and the API key come from environment variables.

## 🧪 Tests

The test files talk to a running API and print PASS or FAIL for every check.

| File | What it checks |
|------|----------------|
| `test.py` | Level 2: key check, validation, trimming and lower casing |
| `test_level3.py` | Level 3: no double booking, cancelling, 20 simultaneous bookings |
| `test_level4.py` | Level 4: safe retries, 10 simultaneous retries, paging through 25 bookings |
| `test_level5.py` | Level 5: the 6th attempt is refused, retries are free, 12 simultaneous attempts |

Set `API_KEY` in the terminal first. To test another address, set `BASE_URL`, which defaults to the local computer in the files that support it.

`test.py` and `test_level4.py` reuse the same email many times. To run them, start the API with `RATE_LIMIT_PER_MINUTE=1000` so the 5 per minute limit does not interfere. The deployed service always uses the real limit of 5.

## ⚠️ Known limitations

1. **The web page contains the API key.** The server puts the key into the page so patients do not need to type it. Anyone who views the page source can read it. A real product would use a separate sign in for patients instead.
2. **Booking requests are handled one at a time per server process.** The booking endpoint runs blocking database calls inside an async function. This is fine at this scale and correctness does not depend on it, because the database enforces the rules, but a busier service would use an async database driver.
3. **Saved retry answers are never cleaned up.** A scheduled clean up of old Idempotency-Key records would keep the table small.
4. **Free hosting sleeps.** The hosting service and the database both pause when idle, so a first request after a quiet period can be slower. A monitor pings the page every 5 minutes to keep the service awake.