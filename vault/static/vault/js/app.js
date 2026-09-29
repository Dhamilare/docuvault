/**
 * DocuVault AI — Core Client Engine
 * Handles CSRF injection, standard fetch wrapper, animated Toast notifications,
 * and global document operations (discard / delete).
 */

// Helper to reliably extract Django CSRF cookie
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

// Global Discard Document action
window.discardDocument = async function (docId) {
  if (!confirm("Are you sure you want to discard this document? The local temporary scan will be permanently deleted.")) {
    return;
  }

  try {
    const { status, data, ok } = await dvFetch(`/api/documents/${docId}/delete/`, {
      method: "POST",
    });

    if (ok && data.ok) {
      dvToast("Document discarded successfully.", "info");

      // 1. Close inspection/review modal if open
      if (typeof closeDocModal === "function") {
        closeDocModal();
      }

      // 2. Remove document rows/cards from the DOM across queues, dropzones, and tables
      const targets = document.querySelectorAll(
        `[data-doc-id="${docId}"], [data-id="${docId}"], #doc-row-${docId}, tr[data-document-id="${docId}"]`
      );
      targets.forEach((el) => {
        el.classList.add("opacity-0", "scale-95", "transition-all", "duration-200");
        setTimeout(() => el.remove(), 200);
      });

      // 3. Decrement review queue badge counters
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
      dvToast(data.error || "Failed to discard document.", "error");
    }
  } catch (err) {
    console.error("Discard error:", err);
    dvToast("An error occurred while discarding the document.", "error");
  }
};

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