"""In-cluster load generator. Reads URL / key from env.  python - < load.py"""
import asyncio
import os
import statistics
import time
from collections import Counter

import httpx

URL = os.environ["TARGET_URL"]
KEY = os.environ["WX_KEY"]
TOTAL = int(os.environ.get("TOTAL", "60"))
CONC = int(os.environ.get("CONC", "10"))
QUESTIONS = [f"Should I carry an umbrella in {c}?" for c in
             ["Paris", "Tokyo", "Mumbai", "Dallas", "Madrid", "Sydney", "Cairo", "Lima"]]


async def main():
    sem, lat, codes = asyncio.Semaphore(CONC), [], Counter()

    async def one(i, client):
        async with sem:
            t = time.perf_counter()
            try:
                r = await client.post(f"{URL}/ask", headers={"x-api-key": KEY},
                                      json={"question": QUESTIONS[i % len(QUESTIONS)]})
                codes[r.status_code] += 1
            except Exception as e:
                codes[type(e).__name__] += 1
            lat.append(time.perf_counter() - t)

    start = time.perf_counter()
    async with httpx.AsyncClient(timeout=40) as client:
        await asyncio.gather(*(one(i, client) for i in range(TOTAL)))
    wall = time.perf_counter() - start
    lat.sort()
    p = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))]
    print(f"requests={TOTAL} concurrency={CONC} wall={wall:.1f}s throughput={TOTAL / wall:.1f} req/s")
    print(f"status={dict(codes)}")
    print(f"latency p50={p(.5):.2f}s p95={p(.95):.2f}s max={lat[-1]:.2f}s mean={statistics.mean(lat):.2f}s")


asyncio.run(main())
