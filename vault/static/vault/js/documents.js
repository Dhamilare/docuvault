/**
 * DocuVault AI — Document Modal & Inspection Controller
 * Dynamically loads telemetry detail or verification review partials into #modal-root or #modal-container.
 */

function getModalRoot() {
  return document.getElementById("modal-root") || document.getElementById("modal-container");
}

function showModalLoading(text) {
  const root = getModalRoot();
  if (!root) return;

  root.innerHTML = `
    <div class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-950/80 backdrop-blur-md">
      <div class="glass-card rounded-2xl px-6 py-4 flex items-center space-x-3 text-slate-300 text-xs font-semibold shadow-2xl border border-slate-700">
        <svg class="animate-spin w-4 h-4 text-indigo-400" viewBox="0 0 24 24" fill="none">
          <circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>
          <path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z"></path>
        </svg>
        <span>${text}</span>
      </div>
    </div>
  `;
  root.classList.remove("hidden");
  root.classList.add("flex");
  document.body.style.overflow = "hidden";
}

// 1. Telemetry & Audit Logs Modal
async function openDocModal(docId) {
  const root = getModalRoot();
  if (!root) return;

  showModalLoading("Loading cognitive document telemetry…");

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

// 2. Verification & SharePoint Filing Modal
async function openReviewModal(docId) {
  const root = getModalRoot();
  if (!root) return;

  showModalLoading("Opening document review workspace…");

  try {
    const res = await fetch(`/api/documents/${docId}/review/`, {
      headers: { "X-Requested-With": "XMLHttpRequest" },
    });

    if (!res.ok) {
      dvToast("Failed to load review form.", "error");
      closeDocModal();
      return;
    }

    root.innerHTML = await res.text();
    if (window.lucide) lucide.createIcons();

    // Attach AJAX submit handler to the review form
    const form = root.querySelector("#review-doc-form");
    if (form) {
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const submitBtn = form.querySelector('button[type="submit"]');
        const origText = submitBtn.innerHTML;
        submitBtn.disabled = true;
        submitBtn.innerHTML = `<span>Uploading to SharePoint…</span>`;

        const formData = new FormData(form);
        try {
          const { status, data, ok } = await dvFetch(form.action, {
            method: "POST",
            body: formData,
          });

          if (ok && data.ok) {
            dvToast("Document successfully filed to SharePoint.", "success");
            closeDocModal();

            // Refresh queue or remove reviewed card
            const row = document.querySelector(`[data-doc-id="${docId}"]`);
            if (row) row.remove();
          } else {
            dvToast(data.error || "SharePoint filing failed.", "error");
            submitBtn.disabled = false;
            submitBtn.innerHTML = origText;
          }
        } catch (err) {
          dvToast("Network error filing document.", "error");
          submitBtn.disabled = false;
          submitBtn.innerHTML = origText;
        }
      });
    }
  } catch (err) {
    dvToast("Network error opening review modal.", "error");
    closeDocModal();
  }
}

function closeDocModal() {
  const root = getModalRoot();
  if (root) {
    root.innerHTML = "";
    root.classList.add("hidden");
    root.classList.remove("flex");
  }
  document.body.style.overflow = "";
}

// Expose globally
window.openDocModal = openDocModal;
window.openReviewModal = openReviewModal;
window.closeDocModal = closeDocModal;

// Global hotkeys (ESC to dismiss)
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeDocModal();
});