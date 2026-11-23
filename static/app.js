const challengeSelect = document.querySelector("#challenge");
const spinButton = document.querySelector("#spin");

if (challengeSelect && spinButton) {
  const choices = [...challengeSelect.options].slice(1);
  const message = document.querySelector("#spin-message");
  const wheelStage = document.querySelector("#challenge-wheel-stage");
  const wheel = document.querySelector("#challenge-wheel-slices");
  const wheelResult = document.querySelector("#wheel-result");
  const wheelSize = 360;
  const center = wheelSize / 2;
  const radius = 174;
  const wheelColors = ["#365847", "#9eaf9f", "#718878", "#d6ded4", "#536f5e", "#bdc9bd"];
  const svgNamespace = "http://www.w3.org/2000/svg";
  let wheelRotation = 0;
  let spinInterval;
  let spinTimeout;
  let finishSpin;

  choices.forEach((choice, index) => {
    const sliceAngle = 360 / choices.length;
    const startAngle = -90 + index * sliceAngle;
    const endAngle = startAngle + sliceAngle;
    const pointAt = (angle) => {
      const radians = (angle * Math.PI) / 180;
      return [center + radius * Math.cos(radians), center + radius * Math.sin(radians)];
    };
    const [startX, startY] = pointAt(startAngle);
    const [endX, endY] = pointAt(endAngle);
    const path = document.createElementNS(svgNamespace, "path");
    path.setAttribute("d", `M ${center} ${center} L ${startX} ${startY} A ${radius} ${radius} 0 0 1 ${endX} ${endY} Z`);
    path.setAttribute("fill", wheelColors[index % wheelColors.length]);
    path.setAttribute("stroke", "#f4f3ed");
    path.setAttribute("stroke-width", "2");
    wheel.append(path);

    const labelAngle = startAngle + sliceAngle / 2;
    const [labelX, labelY] = pointAt(labelAngle);
    const label = document.createElementNS(svgNamespace, "text");
    label.setAttribute("x", labelX);
    label.setAttribute("y", labelY);
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("dominant-baseline", "middle");
    label.setAttribute("class", "wheel-number");
    label.textContent = String(index + 1);
    wheel.append(label);
  });

  const hub = document.createElementNS(svgNamespace, "circle");
  hub.setAttribute("cx", center);
  hub.setAttribute("cy", center);
  hub.setAttribute("r", "27");
  hub.setAttribute("class", "wheel-hub");
  wheel.append(hub);
  const hubMark = document.createElementNS(svgNamespace, "text");
  hubMark.setAttribute("x", center);
  hubMark.setAttribute("y", center);
  hubMark.setAttribute("text-anchor", "middle");
  hubMark.setAttribute("dominant-baseline", "central");
  hubMark.setAttribute("class", "wheel-hub-mark");
  hubMark.textContent = "✦";
  wheel.append(hubMark);
  wheel.style.transform = "rotate(0deg)";

  wheel.addEventListener("transitionend", (event) => {
    if (event.target === wheel && event.propertyName === "transform") finishSpin?.();
  });

  spinButton.addEventListener("click", () => {
    if (!choices.length) return;
    wheelStage.hidden = false;
    spinButton.setAttribute("aria-expanded", "true");
    spinButton.disabled = true;
    spinButton.classList.add("spinning");
    wheelResult.setAttribute("aria-live", "off");
    message.classList.remove("spin-result");

    const selectedIndex = Math.floor(Math.random() * choices.length);
    let cyclingIndex = selectedIndex;
    wheelResult.textContent = choices[cyclingIndex].text;
    challengeSelect.selectedIndex = cyclingIndex + 1;
    spinInterval = window.setInterval(() => {
      cyclingIndex = (cyclingIndex + 1 + Math.floor(Math.random() * (choices.length - 1))) % choices.length;
      wheelResult.textContent = choices[cyclingIndex].text;
      challengeSelect.selectedIndex = cyclingIndex + 1;
    }, 120);

    const sliceAngle = 360 / choices.length;
    const selectedCenter = -90 + (selectedIndex + 0.5) * sliceAngle;
    const currentAngle = ((wheelRotation % 360) + 360) % 360;
    const targetAngle = ((-90 - selectedCenter) % 360 + 360) % 360;
    const extraRotation = ((targetAngle - currentAngle) % 360 + 360) % 360;
    wheelRotation += 360 * 6 + extraRotation;

    finishSpin = () => {
      window.clearInterval(spinInterval);
      window.clearTimeout(spinTimeout);
      finishSpin = null;
      challengeSelect.selectedIndex = selectedIndex + 1;
      spinButton.disabled = false;
      spinButton.classList.remove("spinning");
      wheelResult.textContent = choices[selectedIndex].text;
      wheelResult.setAttribute("aria-live", "polite");
      message.textContent = "Your challenge is set. Go catch that moment!";
      message.classList.add("spin-result");
    };
    // Wait for the newly revealed wheel to paint at its current angle first.
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        wheel.style.transform = `rotate(${wheelRotation}deg)`;
        spinTimeout = window.setTimeout(() => finishSpin?.(), 5200);
      });
    });
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
