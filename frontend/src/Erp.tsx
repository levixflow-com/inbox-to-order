import { useEffect, useRef, useState } from "react";
import { api, ApiError, ERP_CHANNEL } from "./api";
import type { ErpOrder } from "./types";

// The Demo ERP's own screen. It belongs to the Demo ERP, not to Inbox to Order: it reads the ERP's
// database (read-only) and checks every 2 seconds, so a write made through MCP shows up by itself.

const POLL_MS = 2000;

function stamp(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function money(v: string | null): string {
  return v ? `$${Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : "";
}

/** SKUs named in a history entry, e.g. "HS-1102: 6 case -> 8 case" or "added HS-2210 3 box". */
function skusIn(changes: string[]): Set<string> {
  const out = new Set<string>();
  for (const c of changes) for (const m of c.matchAll(/\b([A-Z]{2,}-\d{3,})\b/g)) out.add(m[1]);
  return out;
}

export function Erp() {
  const [orders, setOrders] = useState<ErpOrder[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [fresh, setFresh] = useState<Record<string, number>>({}); // order id -> version to flash
  const [error, setError] = useState<string | null>(null);
  const [checked, setChecked] = useState<Date | null>(null);
  const seen = useRef<Map<string, string> | null>(null);
  // Changes found while this tab was hidden: flashed when it becomes visible, so the change is seen.
  const unseen = useRef<Record<string, number>>({});

  useEffect(() => {
    let stop = false;
    async function poll() {
      try {
        const data = await api<{ orders: ErpOrder[] }>("/erp/api/orders");
        if (stop) return;
        const before = seen.current;
        const now = new Map(data.orders.map((o) => [o.order_id, o.updated_at]));
        if (before) {
          const changed = data.orders.filter((o) => before.get(o.order_id) !== o.updated_at);
          if (changed.length) {
            const versions = Object.fromEntries(changed.map((o) => [o.order_id, o.version]));
            if (document.hidden) unseen.current = { ...unseen.current, ...versions };
            else setFresh((f) => ({ ...f, ...versions }));
            setSelected(changed[0].order_id);
          }
        }
        seen.current = now;
        setOrders(data.orders);
        setChecked(new Date());
        setError(null);
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e));
      }
    }
    async function onVisible() {
      if (document.hidden) return;
      await poll(); // timers of background tabs are slowed down by the browser: check at once
      if (Object.keys(unseen.current).length) {
        const versions = unseen.current;
        unseen.current = {};
        setFresh((f) => ({ ...f, ...versions }));
      }
    }
    // Inbox to Order announces each Demo ERP write in this browser, so this window does not wait for the timer.
    const channel = "BroadcastChannel" in window ? new BroadcastChannel(ERP_CHANNEL) : null;
    if (channel) channel.onmessage = () => void poll();
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onVisible);
    poll();
    const id = window.setInterval(poll, POLL_MS);
    return () => {
      stop = true;
      window.clearInterval(id);
      channel?.close();
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onVisible);
    };
  }, []);

  const order = orders?.find((o) => o.order_id === selected) ?? orders?.[0] ?? null;
  const latest = order?.history.at(-1);
  const changedSkus = latest && latest.version > 1 ? skusIn(latest.changes) : new Set<string>();

  return (
    <div className="erp">
      <header className="erp-top">
        <span className="erp-brand">Demo ERP</span>
        <nav>
          <span className="erp-nav on">Sales orders</span>
          <span className="erp-nav">Customers</span>
          <span className="erp-nav">Catalog</span>
        </nav>
        <span className="erp-live" aria-live="polite">
          <span className="erp-pulse" aria-hidden="true" /> Live{checked ? `, checked ${checked.toLocaleTimeString("en-US")}` : ""}
        </span>
      </header>
      <p className="erp-note">A fictional order system for the Inbox to Order demo. Orders change only through its MCP server (create_order, update_order).</p>
      {error && <p className="erp-error">{error}</p>}

      <div className="erp-main">
        <section className="erp-list" aria-label="Sales orders">
          <table>
            <thead>
              <tr>
                <th>Order</th>
                <th>Customer</th>
                <th>Customer PO</th>
                <th className="num">Lines</th>
                <th className="num">Total</th>
                <th className="num">Version</th>
                <th>Last change</th>
              </tr>
            </thead>
            <tbody>
              {orders?.map((o) => (
                <tr
                  key={`${o.order_id}:${o.version}`}
                  className={`${o.order_id === order?.order_id ? "sel" : ""} ${fresh[o.order_id] === o.version ? "fresh" : ""}`}
                  onClick={() => setSelected(o.order_id)}
                  tabIndex={0}
                  onKeyDown={(e) => e.key === "Enter" && setSelected(o.order_id)}
                >
                  <td className="mono">{o.order_id}</td>
                  <td>{o.customer_name}</td>
                  <td className="mono">{o.po_number}</td>
                  <td className="num">{o.lines.length}</td>
                  <td className="num">{money(o.total_usd)}</td>
                  <td className="num">{o.version}</td>
                  <td>{stamp(o.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {orders && orders.length === 0 && <p className="erp-empty">No orders.</p>}
        </section>

        {order && (
          <section className={`erp-detail${fresh[order.order_id] === order.version ? " fresh" : ""}`} key={`${order.order_id}:${order.version}`} aria-label={`Order ${order.order_id}`}>
            <div className="erp-dhead">
              <span className="erp-id">{order.order_id}</span>
              <span className="erp-ver">Version {order.version}</span>
            </div>
            <dl className="erp-fields">
              <dt>Customer</dt>
              <dd>{order.customer_name}</dd>
              <dt>Customer PO</dt>
              <dd className="mono">{order.po_number}</dd>
              <dt>Ship to</dt>
              <dd className={latest?.changes.some((c) => c.startsWith("ship-to")) && latest.version > 1 ? "chg" : ""}>{order.ship_to.one_line}</dd>
              <dt>Created</dt>
              <dd>{stamp(order.created_at)}</dd>
            </dl>
            <table className="erp-lines">
              <thead>
                <tr>
                  <th className="num">#</th>
                  <th>Item</th>
                  <th className="num">Qty</th>
                  <th>Unit</th>
                  <th className="num">Price</th>
                </tr>
              </thead>
              <tbody>
                {order.lines.map((l) => (
                  <tr key={l.line_no} className={changedSkus.has(l.sku) ? "chg" : ""}>
                    <td className="num">{l.line_no}</td>
                    <td>
                      <span className="mono">{l.sku}</span> {l.name}
                    </td>
                    <td className="num">{l.quantity}</td>
                    <td>{l.unit}</td>
                    <td className="num">{money(l.unit_price)}</td>
                  </tr>
                ))}
              </tbody>
              {order.total_usd && (
                <tfoot>
                  <tr>
                    <td colSpan={4}>Total (fictional prices)</td>
                    <td className="num">{money(order.total_usd)}</td>
                  </tr>
                </tfoot>
              )}
            </table>
            <h3>Change history</h3>
            <ol className="erp-hist">
              {[...order.history].reverse().map((h) => (
                <li key={h.version} className={h === latest ? "last" : ""}>
                  <span className="erp-hv">v{h.version}</span>
                  <span>
                    <b>{h.action === "created" ? "Created" : "Updated"}</b> {stamp(h.at)}
                    {h.changes.length > 0 && (
                      <ul>
                        {h.changes.map((c) => (
                          <li key={c} className="mono">
                            {c.replace(" -> ", " → ")}
                          </li>
                        ))}
                      </ul>
                    )}
                    {h.source_note && <span className="erp-src">{h.source_note}</span>}
                  </span>
                </li>
              ))}
            </ol>
          </section>
        )}
      </div>
    </div>
  );
}
