
function copySec(id){
  var t=document.getElementById('src-'+id), btn=event.currentTarget;
  var done=function(){var o=btn.textContent;btn.textContent='복사됨';setTimeout(function(){btn.textContent=o;},1200);};
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(t.value).then(done,function(){t.hidden=false;t.select();document.execCommand('copy');t.hidden=true;done();});
  }else{t.hidden=false;t.select();document.execCommand('copy');t.hidden=true;done();}
}
function pdfSec(id){
  // 그 섹션만 펼치고, 나머지는 인쇄에서 숨긴다. 인쇄 대화창에서 'PDF로 저장'을 고르면 된다.
  document.querySelectorAll('details.sec').forEach(function(d){d.classList.remove('pt');});
  var d=document.getElementById('sec-'+id); if(!d) return;
  d.classList.add('pt'); d.open=true;
  document.body.classList.add('printing'); window.print();
  setTimeout(function(){document.body.classList.remove('printing');},300);
}
// 카드(목록)에서 기사 원문 복사. 원문 마크다운을 클립보드로.
function copyCard(key){
  var t=document.getElementById('md-'+key), btn=event.currentTarget;
  if(!t) return;
  var done=function(){var o=btn.textContent;btn.textContent='복사됨';setTimeout(function(){btn.textContent=o;},1200);};
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(t.value).then(done,function(){t.hidden=false;t.select();document.execCommand('copy');t.hidden=true;done();});
  }else{t.hidden=false;t.select();document.execCommand('copy');t.hidden=true;done();}
}
// 카드 PDF 버튼이 ?print=article 로 회차 페이지를 열면, 로드 즉시 그 섹션 인쇄를 띄운다.
(function(){
  var q=new URLSearchParams(location.search).get('print');
  if(q) window.addEventListener('load',function(){setTimeout(function(){pdfSec(q);},350);});
})();
// 떠있는 메뉴: 타임스탬프 표시 토글(기본 켜짐, 브라우저에 기억) + 맨 위로.
function slowTop(){ window.scrollTo({top:0,behavior:'smooth'}); }
function slowToggleTS(){
  var nowShow = document.body.classList.toggle('hide-ts') === false;  // 방금 hide-ts를 뗐으면 표시
  try{ localStorage.setItem('ts_show', nowShow?'1':'0'); }catch(e){}
  var b=document.getElementById('fab-ts'); if(b) b.classList.toggle('on', nowShow);
}
(function(){
  // 서버가 정한 기본값(프로파일별로 다르다)을 사용자의 저장된 선택이 덮는다.
  // 국정감사는 서버가 hide-ts를 붙여 내지만, 켠 적('1')이 있으면 그 선택이 이긴다.
  try{
    var v=localStorage.getItem('ts_show');
    if(v==='0') document.body.classList.add('hide-ts');
    else if(v==='1') document.body.classList.remove('hide-ts');
  }catch(e){}
  window.addEventListener('load',function(){
    var b=document.getElementById('fab-ts'); if(!b) return;
    if(!document.querySelector('.ts')){ b.style.display='none'; return; }  // 타임스탬프 없는 페이지면 숨김
    b.classList.toggle('on', !document.body.classList.contains('hide-ts'));
  });
})();
