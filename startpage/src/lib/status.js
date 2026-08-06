// Instance status — the FOUR states the status bar must distinguish (§10), so an
// auth reject, a stale mirror, a closed browser and "never seen" never all look
// like one grey instance. A fifth, healthy, value ('ok') is the connected+fresh
// default. Pure so the classification is unit-testable without a clock.
//
//   rejected — reject_reason set (auth / protocol / origin / duplicate), with time
//   never    — never seen (no last_seen_at)
//   closed   — seen before but not currently connected (browser closed)
//   stale    — connected but the mirror is older than STATE_FRESH-ish (half-open)
//   ok       — connected and fresh

// `now` MUST be in the SERVER clock scale (store.serverNow(), derived from
// StateResponse.server_now): `snapshot_at` / `last_seen_at` are server stamps, and
// comparing them to the laptop's Date.now() turns a couple of seconds of clock drift
// into "зеркало устарело" on every instance forever — or hides a real half-open
// socket. The store owns the offset; this function stays pure.
export function instanceStatus(inst, now, staleMs) {
  if (!inst) return { state: "never", label: "никогда не подключался" };
  if (inst.reject_reason) {
    return { state: "rejected", label: `отклонён: ${inst.reject_reason}`, at: inst.reject_at };
  }
  if (inst.last_seen_at == null) {
    return { state: "never", label: "никогда не подключался" };
  }
  if (!inst.connected) {
    return { state: "closed", label: "закрыт", at: inst.last_seen_at };
  }
  if (inst.snapshot_at == null || now - inst.snapshot_at >= staleMs) {
    return { state: "stale", label: "подключён, зеркало устарело", at: inst.snapshot_at };
  }
  return { state: "ok", label: "на связи", at: inst.snapshot_at };
}

// The `resume_pending` latch has exactly ONE meaning now (§7 "Порог действий на
// проход"): the last pass's plan exceeded the per-pass action threshold
// (MAX_ACTIONS_PER_PASS) and waits for one confirming click. It is SELF-REFRESHING —
// every pass recomputes the plan against the fresh mirror and rewrites the key, so
// what is on screen is at most one pass-interval stale — and SELF-CLEARING: a plan
// that shrank below the threshold drops the latch without any click. Phase B keeps
// executing under the latch (completions of already-confirmed relocations are not
// countable actions), so the notice must not read as "everything stopped".
//
// One short line, no explain paragraph: the plan counts rendered next to the button
// already say what the click will do.
export function planGateNotice(plan) {
  const num = (v) => (typeof v === "number" ? v : null);
  const total = plan ? num(plan.total) : null;
  const threshold = plan ? num(plan.threshold) : null;
  return {
    title: "Ждёт подтверждения",
    sub:
      total != null && threshold != null
        ? `план ${total} действий при пороге ${threshold}`
        : "план превысил порог действий",
  };
}

// "кэш от <время>" and similar relative labels use a plain local time string.
export function formatTime(ts) {
  if (ts == null) return "—";
  try {
    return new Date(ts).toLocaleTimeString();
  } catch {
    return "—";
  }
}

// Date AND time, for stamps that can be days old. The stop is indefinite (§7), so
// "— с 14:05" on a stop pressed last Tuesday reads as "today"; the stopped row needs
// the date. An explicit dd.mm.yyyy hh:mm rather than toLocaleString: deterministic
// across runtimes, and the row must not grow a seconds-ticking tail.
export function formatDateTime(ts) {
  if (ts == null) return "—";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "—";
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}
