// One real three-job acceptance through localhost HTTPS. Never retries a run.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

const native = String.raw`\\wsl.localhost\Ubuntu\home\demus\.local\share\monotrajectoryweb`;
const root = process.argv[2];
assert.match(root ?? '', /^\/home\/demus\/\.local\/share\/monotrajectoryweb\/web\/reports\/queue-[0-9a-f]{32}$/);
const directory = path.join(native, root.split('/monotrajectoryweb/')[1]);
const base = 'https://127.0.0.1:8443';
const credentials = JSON.parse(await fs.readFile(path.join(directory,'credentials.json'),'utf8'));
const secrets = new Set([credentials.password]);
const clean = value => { let result=String(value); for (const secret of secrets) result=result.replaceAll(secret,'[redacted]'); return result; };
const report = {started_at:new Date().toISOString(), passed:false, ignoreHTTPSErrors:true, requests:[], observations:[], jobs:{}, checks:{}, errors:[]};
const save = () => fs.writeFile(path.join(directory,'browser.json'),JSON.stringify(report,null,2));
const {chromium} = await import(pathToFileURL(path.join(native,'frontend-deps/node_modules/playwright-core/index.mjs')).href);
const browser = await chromium.launch({channel:'chrome',headless:true});
const context = await browser.newContext({ignoreHTTPSErrors:true,viewport:{width:1440,height:1100},locale:'uk-UA'});
await context.route('**/*',route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
const page = await context.newPage();
page.on('pageerror',error=>report.errors.push(clean(error.message)));
page.on('console',msg=>{ if (/mixed content/i.test(msg.text())) report.errors.push(clean(msg.text())); });
let csrf;
async function request(method,url,options={}) {
  const started=performance.now();
  const response=await context.request.fetch(base+url,{method,...options,headers:{Origin:base,...(method==='POST'?{'X-CSRF-Token':csrf}:{}),...options.headers}});
  report.requests.push({at:new Date().toISOString(),method,path:url,http:response.status(),seconds:(performance.now()-started)/1000});
  return response;
}
function wsl(args,input) {
  return new Promise((resolve,reject)=>{
    const child=spawn('wsl.exe',['-d','Ubuntu','--exec',...args],{windowsHide:true,stdio:['pipe','pipe','pipe']});
    let output='',errors=''; child.stdout.on('data',data=>output+=data); child.stderr.on('data',data=>errors+=data);
    child.on('error',reject); child.on('close',code=>code===0?resolve(output):reject(new Error(clean(errors||output))));
    child.stdin.end(input ?? '');
  });
}
const helper = (action,input) => wsl(['env','PYTHONPATH=backend:backend/tests','/home/demus/.local/share/monotrajectoryweb/web-venv/bin/python','backend/tests/queue_e2e.py',action,'--root',root],input);
const writeJobs=()=>fs.writeFile(path.join(directory,'jobs.json'),JSON.stringify(report.jobs));
async function observation() {
  try { return JSON.parse(await fs.readFile(path.join(directory,'observation.json'),'utf8')); } catch { return null; }
}
async function noFault() {
  try { const fault=await fs.readFile(path.join(directory,'observer-error.json'),'utf8'); throw new Error(fault); }
  catch(error) { if(error.code!=='ENOENT') throw error; }
}
try {
  assert.equal((await request('GET','/health')).status(),200);
  const bootstrap=await (await request('GET','/api/auth/session')).json(); csrf=bootstrap.csrf_token; secrets.add(csrf);
  const registration=await request('POST','/api/auth/register',{data:credentials}); assert.equal(registration.status(),201);
  const auth=await registration.json(); csrf=auth.csrf_token; secrets.add(csrf);
  report.test_user={id:auth.user.id,login:auth.user.login};
  const cookie=(await context.cookies()).find(cookie=>cookie.name==='mtw_session'); assert.ok(cookie.secure&&cookie.httpOnly); secrets.add(cookie.value);
  await helper('session',JSON.stringify({cookie:cookie.value,csrf}));
  for(const [label,seconds] of [['A',10],['B',12]]) {
    const file=path.join(native,`datasets/freiburg1_xyz-rgb-30fps-short-${seconds}s.mp4`);
    const parameters=await fs.readFile(file.replace(/\.mp4$/,'.parameters.json'),'utf8');
    const response=await request('POST','/api/jobs',{multipart:{video:{name:path.basename(file),mimeType:'video/mp4',buffer:await fs.readFile(file)},parameters}});
    assert.equal(response.status(),202); const job=await response.json();
    report.jobs[label]={id:job.id,frames:seconds*30,created_at:job.created_at}; await writeJobs(); await save();
  }
  const deadline=Date.now()+100000;
  while(true) {
    await noFault(); const sample=await observation();
    if(sample && ['A','B'].every(k=>sample.jobs[k]?.native && sample.jobs[k]?.processed_frames>0)) {
      assert.notEqual(sample.jobs.A.server_id,sample.jobs.B.server_id); report.two_native_before_third=sample; break;
    }
    assert.ok(!sample || !Object.values(sample.jobs).some(j=>['failed','timed_out','cancelled','succeeded'].includes(j.status)),'Pair ended before third submission');
    assert.ok(Date.now()<deadline,'Native tracking did not begin'); await delay(200);
  }
  await page.goto(base); await page.locator('.upload h1').waitFor();
  await page.locator('#video').setInputFiles(path.join(native,'datasets/freiburg1_xyz-rgb-30fps-short-10s.mp4'));
  await page.locator('#calibration').fill(await fs.readFile(path.join(native,'datasets/freiburg1_xyz-rgb-30fps-short-10s.parameters.json'),'utf8'));
  const posted=page.waitForResponse(r=>r.url()===base+'/api/jobs' && r.request().method()==='POST');
  await page.getByRole('button',{name:'Створити задачу',exact:true}).click();
  const response=await posted; assert.equal(response.status(),202); const third=await response.json();
  report.jobs.C={id:third.id,frames:300,created_at:third.created_at}; await writeJobs(); await save();
  await page.locator('.queue-info').waitFor();
  const queued=await page.evaluate(async id=>(await fetch(`/api/jobs/${id}`)).json(),third.id);
  assert.equal(queued.status,'queued'); assert.equal(queued.queue_position,1);
  assert.equal(typeof queued.estimated_wait_seconds,'number'); assert.ok(queued.estimated_wait_seconds>=0);
  report.first_queued={at:new Date().toISOString(),state:queued,text:await page.locator('.queue-info').innerText()};
  await page.screenshot({path:path.join(directory,'queued.png'),fullPage:true});
  const beforeRestart=await observation();
  await wsl(['bash','scripts/stop','dispatcher']); await wsl(['bash','scripts/start','dispatcher']);
  report.dispatcher_restart={at:new Date().toISOString(),before:beforeRestart,after:await observation()};
  assert.equal((await request('GET',`/api/jobs/${third.id}`)).status(),200);
  const seen=new Set(['queued']);
  const completeDeadline=Date.now()+240000;
  while(true) {
    await noFault();
    const sample={at:new Date().toISOString(),jobs:{},ui_heading:await page.locator('.job-summary h1').innerText()};
    for(const [label,entry] of Object.entries(report.jobs)) {
      const response=await request('GET',`/api/jobs/${entry.id}`); assert.equal(response.status(),200);
      const state=await response.json();
      sample.jobs[label]={status:state.status,server_id:state.server_id,queue_position:state.queue_position,estimated_wait_seconds:state.estimated_wait_seconds,started_at:state.started_at,finished_at:state.finished_at};
      assert.ok(!['failed','timed_out','cancelled'].includes(state.status),`${label}: ${state.status} ${state.error}`);
    }
    if(sample.ui_heading==='Обробка') seen.add('running');
    if(sample.ui_heading==='Готово') seen.add('succeeded');
    report.observations.push(sample); await save();
    if(Object.values(sample.jobs).every(j=>j.status==='succeeded')) break;
    assert.ok(Date.now()<completeDeadline,'Acceptance time budget exceeded'); await delay(500);
  }
  await page.locator('.result-stats').waitFor({timeout:15000});
  seen.add((await page.locator('.job-summary h1').innerText())==='Готово'?'succeeded':'unknown');
  assert.deepEqual([...seen],['queued','running','succeeded']);
  await page.screenshot({path:path.join(directory,'succeeded.png'),fullPage:true});
  await page.reload(); await page.locator('.result-stats').waitFor();
  report.checks={ui_queued_running_succeeded:true,eta_position_visible:true,api_available:true,result_reload:true};
  report.validation=JSON.parse(await helper('finish'));
  report.timing=JSON.parse(await helper('timing'));
  report.passed=true;
} catch(error) {
  report.error=clean(error.stack||error);
  for(const entry of Object.values(report.jobs)) { try { await request('POST',`/api/jobs/${entry.id}/cancel`); } catch {} }
  throw new Error(report.error);
} finally {
  report.finished_at=new Date().toISOString(); await save();
  await fs.writeFile(path.join(directory,'stop'),'stop');
  if(csrf) { try { await request('POST','/api/auth/logout'); } catch {} }
  await browser.close();
}
assert.deepEqual(report.errors,[]);
console.log(JSON.stringify({passed:report.passed,jobs:report.jobs,checks:report.checks,first_eta:report.first_queued.state.estimated_wait_seconds,root}));
