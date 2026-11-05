import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {createHmac} from 'node:crypto';
import {pathToFileURL} from 'node:url';
const native=String.raw`\\wsl.localhost\Ubuntu\home\demus\.local\share\monotrajectoryweb`;
const [fixtureRoot,evidenceRoot]=process.argv.slice(2);
assert.match(fixtureRoot??'',/\/auth-browser-[a-f0-9]{32}$/);
assert.match(evidenceRoot??'',/\/admin-[a-f0-9]{32}$/);
const local=r=>path.join(native,r.split('/monotrajectoryweb/')[1]);
const dir=local(fixtureRoot),output=local(evidenceRoot);
const fixture=JSON.parse(await fs.readFile(path.join(dir,'credentials.json'),'utf8'));
const user=fixture.users.a;const seed=await fs.readFile(path.join(dir,'fixture-seed'));
const password=createHmac('sha256',seed).update(user.login).digest('hex');
const {chromium}=await import(pathToFileURL(path.join(native,'frontend-deps/node_modules/playwright-core/index.mjs')).href);
const browser=await chromium.launch({channel:'chrome',headless:true});
const context=await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:1440,height:1000}});
const report={passed:false,ignoreHTTPSErrors:true,errors:[]};
await context.route('**/*',route=>['127.0.0.1','localhost'].includes(new URL(route.request().url()).hostname)?route.continue():route.abort());
const page=await context.newPage();page.on('pageerror',e=>report.errors.push(e.message.replaceAll(password,'[redacted]')));
try{
  await page.goto(fixture.base);await page.locator('#login').fill(user.login);await page.locator('#password').fill(password);
  await page.getByRole('button',{name:'Увійти',exact:true}).click();await page.getByRole('link',{name:'Моніторинг',exact:true}).click();
  await page.getByRole('heading',{name:'Панель адміністратора'}).waitFor();
  await fs.writeFile(path.join(output,'container-review-browser-ready'),'ready');
  const deadline=Date.now()+60000;
  while(Date.now()<deadline){
    const response=await page.waitForResponse(r=>r.url()===fixture.base+'/api/admin/monitoring'&&r.status()===200);
    const data=await response.json();const processes=data.processes.filter(p=>p.server_id==='compute-3');
    if(!processes.some(p=>p.role==='compute'&&p.pid_scope==='container:compute-3'))continue;
    assert.equal(processes.filter(p=>p.role==='compute').length,1);
    assert.equal(processes.filter(p=>p.role==='container_runtime'&&p.pid_scope==='host').length,1);
    assert.equal(new Set(data.processes.map(p=>p.host_pid)).size,data.processes.length);
    await page.locator('tr[data-role="container_runtime"]').waitFor();
    report.processes=processes;report.sampled_at=data.sampled_at;report.passed=true;
    await page.screenshot({path:path.join(output,'admin-container-reviewed.png'),fullPage:true});
    await fs.writeFile(path.join(output,'container-review-viewed'),'viewed');break;
  }
  assert.equal(report.passed,true);assert.deepEqual(report.errors,[]);
}finally{
  await fs.writeFile(path.join(output,'container-review-browser.json'),JSON.stringify(report,null,2));
  await context.close();await browser.close();console.log(JSON.stringify({passed:report.passed,errors:report.errors}));
}
