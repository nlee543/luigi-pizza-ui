// Progressive enhancement only: every ordering action uses a normal HTML form.
document.querySelectorAll('time[datetime]').forEach((time) => {
  const date = new Date(time.dateTime);
  if (!Number.isNaN(date.valueOf())) time.textContent = date.toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'});
});

document.querySelectorAll('[data-submit-once]').forEach((form) => {
  form.addEventListener('submit', () => {
    const button = form.querySelector('button[type="submit"], button:not([type])');
    if (button) { button.disabled = true; button.textContent = 'Sending to the kitchen…'; }
  });
});
window.addEventListener('pageshow', (event) => { if (event.persisted) window.location.reload(); });

const tracker = document.querySelector('[data-tracker]');
if (tracker) {
  const progress = {order_received: 0, preparing: 1, out_for_delivery: 2, ready_for_pickup: 2, completed: 3};
  const message = tracker.querySelector('[data-poll-message]');
  let stopped = false;
  function paint(status) {
    const index = progress[status];
    tracker.dataset.status = status;
    tracker.querySelectorAll('[data-step]').forEach((step, i) => {
      step.classList.toggle('done', i < index);
      step.classList.toggle('current', i === index);
      if (i === index) step.setAttribute('aria-current', 'step');
      else step.removeAttribute('aria-current');
    });
    const fulfillment = tracker.querySelector('[data-fulfillment-label]');
    fulfillment.textContent = status === 'out_for_delivery' ? 'Out for delivery' : status === 'ready_for_pickup' ? 'Ready for pickup' : 'Pickup / delivery';
    if (status === 'completed') { stopped = true; message.textContent = 'Your order is complete. Enjoy!'; }
  }
  paint(tracker.dataset.status);
  async function refresh() {
    if (stopped) return;
    if (!document.hidden) {
      try {
        const response = await fetch(tracker.dataset.pollUrl, {headers: {'Accept': 'application/json'}, cache: 'no-store'});
        if (!response.ok) {
          if (response.status === 401 || response.status === 403) { stopped = true; message.textContent = 'Your session needs attention. Reload this page to continue.'; return; }
          throw new Error('Unavailable');
        }
        const data = await response.json();
        tracker.querySelector('[data-status-label]').textContent = data.label;
        message.textContent = 'Updated ' + new Date().toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'}) + ' · Refreshes every 15 seconds';
        paint(data.status);
      } catch { message.textContent = 'Connection interrupted. Showing the last known status; trying again shortly.'; }
    }
    if (!stopped) window.setTimeout(refresh, 15000);
  }
  if (!stopped) window.setTimeout(refresh, 15000);
}
