/**
 * DocuVault AI — Documents & Review Queue Controller
 */

// 1. CSRF Helper
function getCsrfToken() {
  if (window.CSRF_TOKEN) return window.CSRF_TOKEN;
  if (document.cookie && document.cookie !== "") {
    const cookies = document.cookie.split(";");
    for (let i = 0; i < cookies.length; i++) {
      const cookie = cookies[i].trim();
      if (cookie.startsWith("csrftoken=")) {
        return decodeURIComponent(cookie.substring("csrftoken=".length));
      }
    }
  }
  return document.querySelector("[name=csrfmiddlewaretoken]")?.value || "";
}

// 2. Universal Fetch Wrapper
async function dvFetch(url, options = {}) {
  const opts = {
    method: options.method || "GET",
    headers: {
      "X-Requested-With": "XMLHttpRequest",
      ...(options.headers || {}),
    },
    body: options.body,
  };

  if (opts.method !== "GET") {
    const token = getCsrfToken();
    if (token) opts.headers["X-CSRFToken"] = token;
  }

  const res = await fetch(url, opts);
  const contentType = res.headers.get("content-type") || "";
  let data = {};
  try {
    data = contentType.includes("application/json") ? await res.json() : await res.text();
  } catch (e) {}

  return { status: res.status, data, ok: res.ok };
}
window.dvFetch = dvFetch;

// 3. Global Delete / Discard Document (Fixes deleteDoc is not defined)
async function deleteDoc(target, optionalId) {
  let docId = optionalId;
  let element = null;

  if (typeof target === "number" || typeof target === "string") {
    docId = target;
  } else if (target && target.nodeType) {
    element = target;
    docId = optionalId || target.dataset.id || target.dataset.docId;
  }

  if (!docId) {
    console.error("deleteDoc: No document ID provided.");
    return;
  }

  if (!confirm("Are you sure you want to discard this document? The local temporary scan will be permanently deleted.")) {
    return;
  }

  const btn = element ? element.closest("button") : null;
  const originalHtml = btn ? btn.innerHTML : null;
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<span class="animate-pulse text-rose-400">Discarding…</span>`;
  }

  try {
    const { status, data, ok } = await dvFetch(`/api/documents/${docId}/delete/`, {
      method: "POST",
    });

    if (ok && data.ok) {
      if (typeof dvToast === "function") {
        dvToast("Document discarded successfully.", "info");
      }

      // Close modal if open
      closeDocModal();

      // Remove row/card from DOM with smooth animation
      const selectors = [
        `[data-doc-id="${docId}"]`,
        `[data-id="${docId}"]`,
        `#doc-row-${docId}`,
        `#doc-card-${docId}`,
      ];
      document.querySelectorAll(selectors.join(",")).forEach((el) => {
        el.classList.add("opacity-0", "scale-95", "transition-all", "duration-200");
        setTimeout(() => el.remove(), 200);
      });

      // Update badge counter
      const badge = document.getElementById("review-queue-badge");
      if (badge) {
        const count = parseInt(badge.textContent, 10) || 0;
        if (count > 1) {
          badge.textContent = count - 1;
        } else {
          badge.remove();
        }
      }
    } else {
      const errMsg = data && data.error ? data.error : "Failed to discard document.";
      alert(errMsg);
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = originalHtml;
      }
    }
  } catch (err) {
    console.error("deleteDoc failed:", err);
    alert("Network error while discarding document.");
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = originalHtml;
    }
  }
}
window.deleteDoc = deleteDoc;
window.discardDocument = deleteDoc;

// 4. Modal Handlers
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

function closeDocModal() {
  const root = getModalRoot();
  if (root) {
    root.innerHTML = "";
    root.classList.add("hidden");
    root.classList.remove("flex");
  }
  document.body.style.overflow = "";
}
window.closeDocModal = closeDocModal;

// Read-only Telemetry Inspector
async function openDocModal(docId) {
  const root = getModalRoot();
  if (!root) return;

  showModalLoading("Loading cognitive document telemetry…");

  try {
    const res = await fetch(`/api/documents/${docId}/detail/`, {
      headers: { "X-Requested-With": "XMLHttpRequest" },
    });

    if (!res.ok) {
      alert("Failed to load document details.");
      closeDocModal();
      return;
    }

    root.innerHTML = await res.text();
    if (window.lucide) lucide.createIcons();
  } catch (err) {
    alert("Network error fetching document details.");
    closeDocModal();
  }
}
window.openDocModal = openDocModal;

// Review & Verification Workspace (With Date & Year Widgets and SharePoint filing)
async function openReviewModal(docId) {
  const root = getModalRoot();
  if (!root) return;

  showModalLoading("Opening document review workspace…");

  try {
    const res = await fetch(`/api/documents/${docId}/review/`, {
      headers: { "X-Requested-With": "XMLHttpRequest" },
    });

    if (!res.ok) {
      alert("Failed to load review form.");
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
            closeDocModal();
            // Remove filed item from DOM or refresh
            const item = document.querySelector(`[data-doc-id="${docId}"], #doc-card-${docId}`);
            if (item) item.remove();
            if (typeof dvRefreshList === "function") dvRefreshList();
          } else {
            alert((data && data.error) || "SharePoint filing failed.");
            submitBtn.disabled = false;
            submitBtn.innerHTML = origText;
          }
        } catch (err) {
          alert("Network error while submitting to SharePoint.");
          submitBtn.disabled = false;
          submitBtn.innerHTML = origText;
        }
      });
    }
  } catch (err) {
    alert("Network error opening review modal.");
    closeDocModal();
  }
}
window.openReviewModal = openReviewModal;

// Dismiss modal with ESC
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeDocModal();
});