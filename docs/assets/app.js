
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
