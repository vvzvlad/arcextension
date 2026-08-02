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

// A live pause countdown label (§7): "HH:MM:SS" / "MM:SS" of remaining time. A
// non-positive / null remaining reads as "00:00" — the row flips to "active" then.
export function formatCountdown(ms) {
  if (ms == null || ms <= 0) return "00:00";
  const total = Math.floor(ms / 1000);
  const s = total % 60;
  const m = Math.floor(total / 60) % 60;
  const h = Math.floor(total / 3600);
  const pad = (n) => String(n).padStart(2, "0");
  return (h > 0 ? pad(h) + ":" : "") + pad(m) + ":" + pad(s);
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
