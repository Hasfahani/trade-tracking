// Adds dashboard browser behavior.
(function () {
    var els = document.querySelectorAll('[data-countup]');
    if (!els.length || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    els.forEach(function (el) {
        var target = parseInt(el.getAttribute('data-countup'), 10);
        if (isNaN(target) || target === 0) return;
        var duration = 900;
        var startTime = null;
        function ease(t) { return t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t; }
        function step(ts) {
            if (!startTime) startTime = ts;
            var progress = Math.min((ts - startTime) / duration, 1);
            el.textContent = Math.floor(ease(progress) * target).toLocaleString();
            if (progress < 1) requestAnimationFrame(step);
            else el.textContent = target.toLocaleString();
        }
        requestAnimationFrame(step);
    });
}());

// 7-day activity bar chart
(function () {
    var ctx = document.getElementById('activity-chart');
    if (!ctx) return;
    var chartData;
    try {
        chartData = {
            labels: JSON.parse(ctx.getAttribute('data-chart-labels') || '[]'),
            counts: JSON.parse(ctx.getAttribute('data-chart-counts') || '[]')
        };
    } catch (_) { return; }
    new Chart(ctx, {
        type: 'bar',
        data: {
            labels: chartData.labels,
            datasets: [{
                data: chartData.counts,
                backgroundColor: 'rgba(118, 228, 214, 0.55)',
                borderColor: 'rgba(118, 228, 214, 0.9)',
                borderWidth: 1,
                borderRadius: 5,
                borderSkipped: false
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: true,
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: 'rgba(11, 16, 26, 0.95)',
                    borderColor: 'rgba(118, 228, 214, 0.28)',
                    borderWidth: 1,
                    titleColor: '#98a4b5',
                    bodyColor: '#f5f7fb',
                    callbacks: {
                        label: function (c) {
                            return c.parsed.y + ' trade' + (c.parsed.y !== 1 ? 's' : '');
                        }
                    }
                }
            },
            scales: {
                x: {
                    grid: { color: 'rgba(255,255,255,0.05)' },
                    ticks: { color: '#98a4b5', font: { family: 'Inter, sans-serif', size: 12 } },
                    border: { color: 'rgba(255,255,255,0.08)' }
                },
                y: {
                    beginAtZero: true,
                    grid: { color: 'rgba(255,255,255,0.05)' },
                    ticks: { color: '#98a4b5', precision: 0, font: { family: 'Inter, sans-serif', size: 12 } },
                    border: { color: 'rgba(255,255,255,0.08)' }
                }
            }
        }
    });
}());
