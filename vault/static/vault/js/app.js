/**
 * DocuVault AI — Unified Client Engine (app.js)
 * Master utilities loaded globally across all pages via base.html.
 */

// =============================================================================
// 1. CSRF & Network Layer
// =============================================================================

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

  const domToken = document.querySelector("[name=csrfmiddlewaretoken]")?.value;
  return domToken || "";
}

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
    if (token) {
      opts.headers["X-CSRFToken"] = token;
    }
  }

  try {
    const res = await fetch(url, opts);
    const contentType = res.headers.get("content-type") || "";

    if (contentType.includes("application/json")) {
      return { status: res.status, data: await res.json(), ok: res.ok };
    }
    return { status: res.status, data: await res.text(), ok: res.ok };
  } catch (err) {
    console.error("dvFetch network failure:", err);
    throw err;
  }
}
window.dvFetch = dvFetch;

// =============================================================================
// 2. Global Document Delete / Discard Handler
// =============================================================================

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

  if (!confirm("Are you sure you want to discard this document? The file will be permanently removed.")) {
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
      dvToast("Document discarded successfully.", "info");

      closeDocModal();

      const selectors = [
        `[data-doc-id="${docId}"]`,
        `[data-id="${docId}"]`,
        `#doc-row-${docId}`,
        `#doc-card-${docId}`,
        `tr[data-document-id="${docId}"]`
      ];
      document.querySelectorAll(selectors.join(",")).forEach((el) => {
        el.classList.add("opacity-0", "scale-95", "transition-all", "duration-200");
        setTimeout(() => el.remove(), 200);
      });

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
      const errMsg = (data && data.error) ? data.error : "Failed to discard document.";
      dvToast(errMsg, "error");
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = originalHtml;
      }
    }
  } catch (err) {
    console.error("deleteDoc failed:", err);
    dvToast("Network error while discarding document.", "error");
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = originalHtml;
    }
  }
}
window.deleteDoc = deleteDoc;
window.discardDocument = deleteDoc;
window.deleteDocument = deleteDoc;

// =============================================================================
// 3. Modal Controllers
// =============================================================================

function getModalRoot() {
  return document.getElementById("modal-container") || document.getElementById("modal-root");
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
window.openDocModal = openDocModal;

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

            const row = document.querySelector(`[data-doc-id="${docId}"], #doc-card-${docId}`);
            if (row) row.remove();
            if (typeof dvRefreshList === "function") dvRefreshList();
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
window.openReviewModal = openReviewModal;

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeDocModal();
});

// =============================================================================
// 4. Toast Notifications
// =============================================================================

function dvToast(message, variant = "info") {
  const host = document.getElementById("toast-host");
  if (!host) return;

  const palettes = {
    info: "bg-slate-900/90 border-slate-700 text-slate-200 shadow-slate-950/50",
    success: "bg-emerald-950/90 border-emerald-700/80 text-emerald-200 shadow-emerald-950/50",
    error: "bg-rose-950/90 border-rose-700/80 text-rose-200 shadow-rose-950/50",
    warning: "bg-amber-950/90 border-amber-700/80 text-amber-200 shadow-amber-950/50",
  };

  const icons = {
    info: '<i data-lucide="info" class="w-4 h-4 text-indigo-400 shrink-0 mt-0.5"></i>',
    success: '<i data-lucide="check-circle-2" class="w-4 h-4 text-emerald-400 shrink-0 mt-0.5"></i>',
    error: '<i data-lucide="alert-octagon" class="w-4 h-4 text-rose-400 shrink-0 mt-0.5"></i>',
    warning: '<i data-lucide="alert-triangle" class="w-4 h-4 text-amber-400 shrink-0 mt-0.5"></i>',
  };

  const el = document.createElement("div");
  el.className = `pointer-events-auto border ${palettes[variant] || palettes.info} rounded-xl backdrop-blur-md shadow-2xl px-4 py-3 text-xs font-semibold flex items-start space-x-2.5 transition-all duration-300 transform translate-y-3 opacity-0`;
  el.innerHTML = `
    ${icons[variant] || icons.info}
    <span class="flex-1 leading-relaxed">${message}</span>
    <button class="text-slate-400 hover:text-white transition p-0.5 ml-1 leading-none" aria-label="Dismiss">&times;</button>
  `;

  el.querySelector("button").addEventListener("click", () => {
    el.classList.add("opacity-0", "translate-y-2");
    setTimeout(() => el.remove(), 250);
  });

  host.appendChild(el);
  if (window.lucide) lucide.createIcons({ root: el });

  requestAnimationFrame(() => {
    el.classList.remove("translate-y-3", "opacity-0");
  });

  setTimeout(() => {
    if (el.parentNode) {
      el.classList.add("opacity-0", "translate-y-2");
      setTimeout(() => el.remove(), 250);
    }
  }, 5500);
}
window.dvToast = dvToast;