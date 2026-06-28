/* Forum Bot Admin – Global JS */

// ─── Sidebar toggle ───────────────────────────────────────────────────────
function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('open');
}

// Click outside sidebar to close (mobile)
document.addEventListener('click', (e) => {
  const sb = document.getElementById('sidebar');
  if (sb && sb.classList.contains('open') && !sb.contains(e.target)) {
    const toggle = document.querySelector('.menu-toggle');
    if (toggle && !toggle.contains(e.target)) {
      sb.classList.remove('open');
    }
  }
});

// ─── Toast notification ───────────────────────────────────────────────────
let _toastTimer = null;

function showToast(msg, duration = 3000) {
  let toast = document.getElementById('toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'toast';
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.classList.add('show');
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => toast.classList.remove('show'), duration);
}

// ─── Copy to clipboard ────────────────────────────────────────────────────
function copyText(text) {
  navigator.clipboard.writeText(text)
    .then(() => showToast('✅ Đã sao chép!'))
    .catch(() => showToast('❌ Không thể sao chép'));
}

// ─── Relative time ────────────────────────────────────────────────────────
function relativeTime(dateStr) {
  if (!dateStr) return '–';
  const diff = Date.now() - new Date(dateStr + 'Z').getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'vừa xong';
  if (mins < 60) return `${mins} phút trước`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs} giờ trước`;
  const days = Math.floor(hrs / 24);
  return `${days} ngày trước`;
}

// Upgrade all .date cells with relative time on hover
document.querySelectorAll('.date').forEach(el => {
  const raw = el.textContent.trim();
  if (raw && raw !== '–') {
    el.title = raw;
    el.textContent = relativeTime(raw);
  }
});

// ─── Auto-refresh stats every 60s ────────────────────────────────────────
if (typeof refreshStats === 'function') {
  setInterval(refreshStats, 60000);
}

// ─── Confirm wrapper ──────────────────────────────────────────────────────
function confirmAction(msg, fn) {
  if (confirm(msg)) fn();
}

// ─── API helpers ──────────────────────────────────────────────────────────
async function apiFetch(url, options = {}) {
  try {
    const res = await fetch(url, options);
    return await res.json();
  } catch (e) {
    showToast(`❌ Lỗi mạng: ${e.message}`);
    return null;
  }
}
