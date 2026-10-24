/* Public showcases are readable; write authorization remains on the server. */
'use strict';
window.configurePublicDemo = function(info) {
  if (!info?.enabled || !info.readOnly || document.body.dataset.publicDemo === 'true') return;
  document.body.dataset.publicDemo = 'true';
  const banner = document.createElement('div');
  banner.className = 'service-notice';
  banner.id = 'public-demo-notice';
  banner.setAttribute('role', 'note');
  banner.textContent = '公开样例浏览 · 旋转结构、查看 PAE、探索已完成批次。新建、导入与运维写操作在此展示环境关闭。';
  document.querySelector('#service-notice').before(banner);
  const selectors = '#new-job-button,#import-button,[data-new-job],[data-cancel],[data-retry],[data-campaign-action],#campaign-submit,#storage-scan-submit,#storage-plan-submit,#storage-plan-apply,#storage-cancel';
  let pending = false;
  const apply = () => {
    pending = false;
    document.querySelectorAll(selectors).forEach(button => {
      if (!button.disabled) button.disabled = true;
      if (button.title !== '公开浏览环境：写操作关闭') button.title = '公开浏览环境：写操作关闭';
    });
  };
  new MutationObserver(() => {
    if (!pending) { pending = true; requestAnimationFrame(apply); }
  }).observe(document.querySelector('#content'), { childList:true, subtree:true });
  apply();
};
