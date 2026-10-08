const {chromium}=require('playwright');
(async()=>{
 const browser=await chromium.launch({headless:true,...(process.env.NEXUS_TEST_BROWSER?{executablePath:process.env.NEXUS_TEST_BROWSER}:{})});
 const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(process.env.NEXUS_TEST_URL||'http://127.0.0.1:30000/',{waitUntil:'networkidle'});
 let answer={type:'confirmation',action_id:'test-edit-transfer',title:'确认转账',detail:'收款人：测试收款人\n金额：100元',editable:['amount','purpose'],terms:{amount:'100',purpose:''},expires_at:new Date(Date.now()+300000).toISOString()};
 let mode='same',confirmed=false;
 await page.route('**/api/actions/test-edit-transfer',async route=>{
  if(mode==='error'){await route.fulfill({status:400,json:{message:'测试金额校验失败'}});return;}
  if(mode==='changed'){const body=route.request().postDataJSON();answer={...answer,terms:{...answer.terms,amount:body.amount},detail:'收款人：测试收款人\n金额：'+body.amount+'元'};}
  await route.fulfill({json:answer});
 });
 await page.route('**/api/actions/test-edit-transfer/confirm',async route=>{confirmed=true;await route.fulfill({json:{type:'message',message:'已收到测试确认请求',action_id:'test-edit-transfer'}});});
 const mount=async()=>{await page.evaluate(a=>{document.querySelectorAll('[data-action-id="test-edit-transfer"]').forEach(el=>el.remove());actionVersions.delete(a.action_id);seenActions.delete(a.action_id);renderAnswer(a);},answer);};
 const card=()=>page.locator('[data-action-id="test-edit-transfer"]');
 const click=async name=>{await card().getByRole('button',{name,exact:true}).click();};
 const assertEnabled=async()=>{for(const name of ['修改这笔','确认执行','取消'])if(!await card().getByRole('button',{name,exact:true}).isEnabled())throw Error('Disabled: '+name);};
 await mount();await click('修改这笔');
 if(!await card().getByRole('button',{name:'收起修改',exact:true}).isEnabled())throw Error('Close toggle disabled');
 await click('不改了');await assertEnabled();console.log('PASS cancel editing restores all three buttons');
 await click('修改这笔');await click('收起修改');await assertEnabled();console.log('PASS close toggle restores buttons');
 await click('修改这笔');await click('保存修改');await page.waitForFunction(()=>!busy);await assertEnabled();
 if(await card().locator('.action-edit-wrap').count())throw Error('Same-payload save left editor open');console.log('PASS unchanged response forces new actionable card');
 mode='changed';await click('修改这笔');await card().locator('[data-edit-field="amount"]').fill('200');await click('保存修改');await page.waitForFunction(()=>!busy);await assertEnabled();
 if(!await card().innerText().then(t=>t.includes('200元')))throw Error('Edited amount not shown');console.log('PASS changed amount refreshes card and buttons');
 mode='error';await click('修改这笔');await click('保存修改');await page.waitForFunction(()=>!busy);
 if(!await card().locator('.action-edit-error').innerText().then(t=>t.includes('失败')))throw Error('Missing validation error');
 await click('不改了');await assertEnabled();console.log('PASS failed save preserves editor and cancel recovers');
 await click('确认执行');await page.waitForFunction(()=>!busy);if(!confirmed)throw Error('Confirm not delivered');console.log('PASS confirm works after editing (intercepted, no transfer)');
 answer={...answer,expires_at:new Date(Date.now()+1000).toISOString()};await mount();await click('修改这笔');await page.waitForTimeout(1400);await click('不改了');
 for(const name of ['修改这笔','确认执行','取消'])if(await card().getByRole('button',{name,exact:true}).isEnabled())throw Error('Expired operation re-enabled');console.log('PASS expiry remains disabled after closing editor');
 if(errors.length)throw Error(errors.join('\n'));await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
