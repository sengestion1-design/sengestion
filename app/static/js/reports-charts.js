/* Rendu des graphiques du module Rapports (Chart.js). Charte stricte : marine/or/jaune pâle. */
(function () {
  const MARINE = "#021A3D";
  const OR = "#F2B10E";
  const JAUNE_PALE = "#E8E7A2";

  const script = document.currentScript;
  const trend = JSON.parse(script.dataset.trend || "null");
  const breakdown = JSON.parse(script.dataset.breakdown || "null");

  if (trend && trend.labels && trend.labels.length) {
    const ctx = document.getElementById("chart-trend");
    if (ctx) {
      new Chart(ctx, {
        type: "bar",
        data: {
          labels: trend.labels,
          datasets: [
            {
              label: "Chiffre d'affaires (FCFA)",
              data: trend.revenue,
              backgroundColor: OR,
            },
            {
              label: "Dépenses (FCFA)",
              data: trend.expenses,
              backgroundColor: MARINE,
            },
          ],
        },
        options: {
          responsive: true,
          plugins: { legend: { position: "bottom" } },
          scales: { y: { beginAtZero: true } },
        },
      });
    }
  }

  if (breakdown && breakdown.labels && breakdown.labels.length) {
    const ctx = document.getElementById("chart-breakdown");
    if (ctx) {
      const palette = [MARINE, OR, JAUNE_PALE, "#4A5D7A", "#C98F0A", "#CFCE84"];
      new Chart(ctx, {
        type: "doughnut",
        data: {
          labels: breakdown.labels,
          datasets: [
            {
              data: breakdown.amounts,
              backgroundColor: breakdown.labels.map((_, i) => palette[i % palette.length]),
            },
          ],
        },
        options: {
          responsive: true,
          plugins: { legend: { position: "bottom" } },
        },
      });
    }
  }
})();
