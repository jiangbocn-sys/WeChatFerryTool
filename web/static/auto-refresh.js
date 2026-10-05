// 通用"每 30 秒自动刷新"开关
// 依赖：HTML 里要有一个 id="auto-refresh-toggle" 的 checkbox
(function () {
    var KEY = 'wft_auto_refresh';
    var cb = document.getElementById('auto-refresh-toggle');
    if (!cb) return;
    cb.checked = localStorage.getItem(KEY) === '1';
    if (cb.checked) start();
    cb.addEventListener('change', function () {
        if (cb.checked) {
            localStorage.setItem(KEY, '1');
            start();
        } else {
            localStorage.setItem(KEY, '0');
            stop();
        }
    });
    function start() { window._autoTimer = setInterval(function () { location.reload(); }, 30000); }
    function stop()  { if (window._autoTimer) clearInterval(window._autoTimer); }
})();
