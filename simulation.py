"""
ARINA latency-hiding simulation.

Every signal in this model travels at the speed of light. Nothing is faster.
The question it answers is: how much of the light-speed delay can a local
"delegate" AI hide by predicting the operator's intent before it arrives?

Model
-----
* The operator makes one decision ("intent") every `interval` seconds.
  Their behaviour is a Markov chain over a small set of intents.
* Each intent is sent as a small, authenticated Q-packet. It arrives at the
  remote node after the one-way light delay d = distance / c.
* The node runs a delegate: an approximate copy of the operator's policy that
  was shipped earlier over the bulk backbone (ARINA-G). `fidelity` (0..1) says
  how close that copy is to the real operator.
* At each step the delegate acts *now*, on its prediction. It only knows the
  operator's intents up to d seconds ago, so it has to predict k = ceil(d/interval)
  steps ahead: it propagates the last confirmed intent through its model k times.
* When the real intent arrives, the node compares:
    - reversible action, prediction right  -> latency 0 (the delay was hidden)
    - reversible action, prediction wrong  -> rolled back, latency d
    - irreversible action                  -> never speculated; executed only on
                                              arrival of the signed intent, latency d
* Scenarios differ in distance and in what a packet carries: moment-to-moment
  commands (change often) or high-level goals (change rarely, sent yearly).
* A safety governor rejects packets with an invalid signature or a command
  above the node's energy limit.

Simplification: the operator's choices do not depend on what they see from
the node (which would itself be d seconds old). The model is about the
prediction problem, not about closed-loop control.

Run:  python simulation.py
      python simulation.py --fidelity 0.7 --steps 20000 --seed 1
      python simulation.py --scenario mars
"""

import argparse
import hashlib
import hmac
import math
import random
import secrets

C = 299_792_458.0                 # speed of light, m/s
AU = 149_597_870_700.0            # astronomical unit, m
LIGHT_YEAR = 9_460_730_472_580_800.0  # m
DAY = 86_400.0
YEAR = 365.25 * DAY

# (name, distance in metres, decision interval in seconds, stickiness)
# stickiness = chance the operator keeps the same intent at the next step.
# Moment-to-moment commands change often; high-level goals change rarely.
SCENARIOS = {
    "moon":          ("Moon, commands",       384_400_000.0,       1.0,  0.6),
    "mars":          ("Mars, commands",       1.52 * AU,           60.0, 0.6),
    "proxima":       ("Proxima, commands",    4.2465 * LIGHT_YEAR, DAY,  0.6),
    "proxima_goals": ("Proxima, goals",       4.2465 * LIGHT_YEAR, YEAR, 0.97),
}

INTENTS = [
    # (name, reversible)
    ("advance",         True),
    ("scan_area",       True),
    ("sample_soil",     True),
    ("return_to_base",  True),
    ("hold",            True),
    ("drill_core",      False),
    ("deploy_beacon",   False),
]
HOLD = 4
REVERSIBLE = [r for _, r in INTENTS]
N = len(INTENTS)

ENERGY_LIMIT_J = 1_000.0          # the node refuses commands above this


# ---------------------------------------------------------------- matrices --

def normalise(row):
    s = sum(row)
    return [x / s for x in row]


def make_operator_policy(rng, stickiness=0.6):
    """Operator's true behaviour: tends to repeat, otherwise prefers a few
    'natural next steps'."""
    m = []
    for i in range(N):
        row = [rng.random() ** 3 for _ in range(N)]   # a few strong preferences
        row = normalise(row)
        row = [(1 - stickiness) * x for x in row]
        row[i] += stickiness
        m.append(row)
    return m


def make_delegate_model(rng, true_policy, fidelity):
    """The delegate's learned copy: true policy blended with noise."""
    m = []
    for row in true_policy:
        noise = normalise([rng.random() for _ in range(N)])
        m.append([fidelity * a + (1 - fidelity) * b for a, b in zip(row, noise)])
    return m


def matmul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(N)) for j in range(N)]
            for i in range(N)]


def matpow(m, k):
    result = [[1.0 if i == j else 0.0 for j in range(N)] for i in range(N)]
    while k:
        if k & 1:
            result = matmul(result, m)
        m = matmul(m, m)
        k >>= 1
    return result


def sample(rng, row):
    x, acc = rng.random(), 0.0
    for i, p in enumerate(row):
        acc += p
        if x < acc:
            return i
    return N - 1


# ---------------------------------------------------------------- security --

class Link:
    """Authenticated packets. The shared key stands in for a key agreed by
    QKD (which itself needs a light-speed classical channel)."""

    def __init__(self):
        self.key = secrets.token_bytes(32)

    def sign(self, payload: bytes) -> bytes:
        return hmac.new(self.key, payload, hashlib.sha256).digest()

    def packet(self, seq, intent, energy_j=10.0):
        payload = f"{seq}|{intent}|{energy_j}".encode()
        return payload, self.sign(payload)


def governor(link, payload, tag):
    """Safety governor on the node. Returns (accepted, reason)."""
    if not hmac.compare_digest(link.sign(payload), tag):
        return False, "invalid signature"
    _, _, energy = payload.decode().split("|")
    if float(energy) > ENERGY_LIMIT_J:
        return False, f"energy {float(energy):.0f} J above limit {ENERGY_LIMIT_J:.0f} J"
    return True, "ok"


# -------------------------------------------------------------- simulation --

def human_time(seconds):
    if seconds < 120:
        return f"{seconds:.2f} s"
    if seconds < 2 * 3600:
        return f"{seconds / 60:.1f} min"
    if seconds < 2 * DAY:
        return f"{seconds / 3600:.1f} h"
    if seconds < 2 * YEAR:
        return f"{seconds / DAY:.0f} days"
    return f"{seconds / YEAR:.2f} years"


def run(scenario, fidelity, steps, seed):
    name, distance, interval, stickiness = SCENARIOS[scenario]
    rng = random.Random(seed)
    link = Link()

    delay = distance / C
    k = max(1, math.ceil(delay / interval))       # steps the delegate must look ahead

    policy = make_operator_policy(rng, stickiness)
    model = make_delegate_model(rng, policy, fidelity)
    model_k = matpow(model, k)

    total = steps + k
    # operator's real intents, decided on Earth
    intents = [HOLD]
    for _ in range(total - 1):
        intents.append(sample(rng, policy[intents[-1]]))

    hits = misses = irreversible = rejected = 0
    latency_sum = 0.0

    for j in range(steps):
        # What the node knows at step j: intents up to j - k (light delay).
        known = j - k
        last_confirmed = intents[known] if known >= 0 else HOLD
        belief = model_k[last_confirmed]
        guess = max(range(N), key=lambda i: belief[i])
        if not REVERSIBLE[guess]:
            guess = HOLD                          # never speculate irreversibly

        # The real intent of step j arrives at step j + k; check it then.
        payload, tag = link.packet(j, INTENTS[intents[j]][0])
        ok, _ = governor(link, payload, tag)
        if not ok:
            rejected += 1
            continue

        actual = intents[j]
        if not REVERSIBLE[actual]:
            irreversible += 1
            latency_sum += delay
        elif guess == actual:
            hits += 1
        else:
            misses += 1
            latency_sum += delay

    reversible = hits + misses
    return {
        "name": name,
        "delay": delay,
        "lookahead": k,
        "hit_rate": hits / reversible if reversible else 0.0,
        "irreversible_share": irreversible / steps,
        "mean_latency": latency_sum / steps,
        "hidden_share": 1 - (latency_sum / steps) / delay,
        "rejected": rejected,
    }


def security_demo():
    print("\n--- Safety governor ---")
    link = Link()
    cases = [
        ("operator", link.packet(1, "sample_soil", 12.0)),
        ("operator, over limit", link.packet(2, "deploy_beacon", 1_500.0)),
    ]
    forged_payload = b"3|override_systems|5.0"
    cases.append(("forged (no key)", (forged_payload, secrets.token_bytes(32))))
    for label, (payload, tag) in cases:
        ok, reason = governor(link, payload, tag)
        verdict = "ACCEPTED" if ok else "REJECTED"
        print(f"  {label:<22} {payload.decode():<32} -> {verdict} ({reason})")


def main():
    p = argparse.ArgumentParser(description="ARINA latency-hiding simulation")
    p.add_argument("--scenario", choices=[*SCENARIOS, "all"], default="all")
    p.add_argument("--fidelity", type=float, default=0.9,
                   help="how close the delegate's model is to the operator (0..1)")
    p.add_argument("--steps", type=int, default=10_000)
    p.add_argument("--seed", type=int, default=42)
    a = p.parse_args()

    print("=== ARINA latency-hiding simulation (everything at c) ===")
    print(f"delegate fidelity {a.fidelity:.2f}, {a.steps} decisions per scenario, seed {a.seed}\n")
    header = f"{'route, packet level':<28}{'one-way delay':>15}{'look-ahead':>12}{'hit rate':>10}{'mean latency':>15}{'delay hidden':>14}"
    print(header)
    print("-" * len(header))
    names = SCENARIOS if a.scenario == "all" else [a.scenario]
    for s in names:
        r = run(s, a.fidelity, a.steps, a.seed)
        print(f"{r['name']:<28}{human_time(r['delay']):>15}{r['lookahead']:>9} st"
              f"{r['hit_rate']:>10.0%}{human_time(r['mean_latency']):>15}{r['hidden_share']:>14.0%}")

    print("\nhit rate      = share of reversible actions the delegate predicted correctly")
    print("mean latency  = average wait between an operator's decision and its effect")
    print("delay hidden  = 1 - mean latency / one-way delay")
    print("Irreversible actions are never predicted: they always wait the full delay.")
    security_demo()


if __name__ == "__main__":
    main()
