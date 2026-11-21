const challengeSelect = document.querySelector("#challenge");
const spinButton = document.querySelector("#spin");

if (challengeSelect && spinButton) {
  const choices = [...challengeSelect.options].slice(1);
  const message = document.querySelector("#spin-message");
  spinButton.addEventListener("click", () => {
    spinButton.disabled = true;
    spinButton.classList.add("spinning");
    let turns = 0;
    const ticker = window.setInterval(() => {
      challengeSelect.selectedIndex = 1 + Math.floor(Math.random() * choices.length);
      turns += 1;
      if (turns >= 14) {
        window.clearInterval(ticker);
        spinButton.disabled = false;
        spinButton.classList.remove("spinning");
        message.textContent = "Your challenge is set. Go catch that moment!";
        message.classList.add("spin-result");
      }
    }, 75 + turns * 5);
  });
}

const photoInput = document.querySelector("#photos");
if (photoInput) {
  photoInput.addEventListener("change", () => {
    const list = document.querySelector("#file-list");
    const count = photoInput.files.length;
    const maxPhotos = photoInput.dataset.maxPhotos;
    const maxMb = photoInput.dataset.maxMb;
    list.textContent = count ? `${count} ${count === 1 ? "photo" : "photos"} ready to send.` : `Up to ${maxPhotos} photos, ${maxMb} MB each.`;
    list.classList.toggle("file-list-ready", count > 0);
  });
}

const countdown = document.querySelector("[data-countdown]");
if (countdown) {
  const target = new Date(countdown.dataset.countdown).getTime();
  const renderCountdown = () => {
    const remaining = Math.max(0, target - Date.now());
    const days = Math.floor(remaining / 86400000);
    const hours = Math.floor((remaining % 86400000) / 3600000);
    const minutes = Math.floor((remaining % 3600000) / 60000);
    countdown.querySelector("[data-days]").textContent = String(days).padStart(2, "0");
    countdown.querySelector("[data-hours]").textContent = String(hours).padStart(2, "0");
    countdown.querySelector("[data-minutes]").textContent = String(minutes).padStart(2, "0");
    if (remaining === 0) window.location.reload();
  };
  renderCountdown();
  window.setInterval(renderCountdown, 30000);
}

const categoryButtons = document.querySelectorAll("[data-filter]");
categoryButtons.forEach((button) => {
  button.addEventListener("click", () => {
    categoryButtons.forEach((item) => item.classList.toggle("active", item === button));
    document.querySelectorAll("[data-category]").forEach((section) => {
      section.hidden = button.dataset.filter !== "all" && section.dataset.category !== button.dataset.filter;
    });
  });
});
