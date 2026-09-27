/**
 * DocuVault AI — Document Modal & Inspection Controller
 * Dynamically loads the document detail partial into #modal-root or #modal-container.
 */

async function openDocModal(docId) {
  const root = document.getElementById("modal-root") || document.getElementById("modal-container");
  if (!root) return;

  root.innerHTML = `
    <div class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-950/80 backdrop-blur-md">
      <div class="glass-card rounded-2xl px-6 py-4 flex items-center space-x-3 text-slate-300 text-xs font-semibold shadow-2xl border border-slate-700">
        <svg class="animate-spin w-4 h-4 text-indigo-400" viewBox="0 0 24 24" fill="none">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path>
        </svg>
        <span>Loading cognitive document telemetry…</span>
      </div>
    </div>
  `;
  root.classList.remove("hidden");
  root.classList.add("flex");
  document.body.style.overflow = "hidden";

  try {
    const res = await fetch(`/api/documents/${docId}/detail/`, {
      headers: { "X-Requested-With": "XMLHttpRequest" },
    });

    if (!res.ok) {
      dvToast("Failed to load document details.", "error");
      closeDocModal();
      return;
    }

    root.innerHTML = await res.text();
    if (window.lucide) lucide.createIcons();
  } catch (err) {
    dvToast("Network error fetching document details.", "error");
    closeDocModal();
  }
}

function closeDocModal() {
  const root = document.getElementById("modal-root") || document.getElementById("modal-container");
  if (root) {
    root.innerHTML = "";
    root.classList.add("hidden");
    root.classList.remove("flex");
  }
  document.body.style.overflow = "";
}

// Global hotkeys (ESC to dismiss)
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeDocModal();
});