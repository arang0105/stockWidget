@echo off
rem 위젯을 화면 밖에서 잃어버렸을 때 우상단 기본 위치로 되돌린다.
rem 위젯이 이미 실행 중이면 하나가 더 뜬다. 그때는 둘 중 하나를 우클릭해서 종료하면 된다.
start "" pyw "%~dp0stock_widget.pyw" --reset
