document.addEventListener("DOMContentLoaded", () => {
  const toggle = document.querySelector("[data-sidebar-toggle]");
  if (toggle) {
    toggle.addEventListener("click", () => {
      document.body.classList.toggle("sidebar-open");
    });
  }

  document.addEventListener("click", (event) => {
    const sidebar = document.getElementById("sidebar");
    if (!sidebar || !document.body.classList.contains("sidebar-open")) {
      return;
    }
    if (sidebar.contains(event.target) || toggle.contains(event.target)) {
      return;
    }
    document.body.classList.remove("sidebar-open");
  });
});
