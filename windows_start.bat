@echo off
:: Se chamado com --server, executar o servidor direto
if "%1"=="--server" goto :server

setlocal EnableDelayedExpansion

:: Usado pelo atalho de inicializacao automatica do Windows. Faz todo o
:: bootstrap normal, mas nao abre o navegador nem deixa uma janela visivel.
set "AUTOSTART=0"
if /I "%1"=="--autostart" set "AUTOSTART=1"

:: Garantir que o diretorio de trabalho eh o mesmo do bat
cd /d "%~dp0"

:: Perguntar somente na primeira abertura manual. A escolha fica salva dentro
:: de storages, que ja e a pasta de dados persistentes do WhatsBot-Lite.
:: Se ja estiver ativado, atualizar o atalho para corrigir versoes anteriores
:: e acompanhar mudancas no caminho da pasta do WhatsBot-Lite.
if "!AUTOSTART!"=="0" (
    if not exist "storages\windows_autostart_choice.txt" (
        call :ask_autostart
    ) else (
        findstr /X /C:"enabled" "storages\windows_autostart_choice.txt" >nul
        if not errorlevel 1 (
            call :install_autostart
            if errorlevel 1 echo [AVISO] Nao foi possivel atualizar o atalho de inicializacao automatica.
        )
    )
)

:: Matar processos anteriores que podem estar pendurados
taskkill /F /IM gowa.exe >nul 2>&1
powershell -Command ^
  "$pids = @(netstat -ano | Select-String 'LISTENING' | Select-String ':8080 ' | ForEach-Object { ($_ -split '\s+')[-1] } | Select-Object -Unique); " ^
  "foreach ($p in $pids) { " ^
  "  Get-CimInstance Win32_Process -Filter \"ParentProcessId=$p\" | ForEach-Object { taskkill /F /PID $_.ProcessId 2>&1 | Out-Null }; " ^
  "  taskkill /F /PID $p 2>&1 | Out-Null " ^
  "}" >nul 2>&1

echo.
echo ========================================
echo   WhatsBot-Lite - Verificando ambiente...
echo ========================================
echo.

:: ===== 1. DETECTAR OU INSTALAR PYTHON =====
set "PYTHON_CMD="
set "PY_FULL_PATH=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"

:: Tentar Python pelo caminho completo primeiro (mais confiavel)
if exist "!PY_FULL_PATH!" (
    "!PY_FULL_PATH!" -c "import encodings" >nul 2>&1
    if !ERRORLEVEL!==0 (
        set "PYTHON_CMD=!PY_FULL_PATH!"
        goto :check_version
    )
    :: Python existe mas esta corrompido — sera reinstalado abaixo
    echo [!] Python encontrado mas com instalacao corrompida. Reinstalando...
    rmdir /s /q "%LOCALAPPDATA%\Programs\Python\Python312" >nul 2>&1
)

:: Tentar "python" do PATH (validando que funciona de verdade)
python -c "import encodings" >nul 2>&1
if !ERRORLEVEL!==0 (
    set "PYTHON_CMD=python"
    goto :check_version
)

:: Tentar "py -3"
py -3 -c "import encodings" >nul 2>&1
if !ERRORLEVEL!==0 (
    set "PYTHON_CMD=py -3"
    goto :check_version
)

:: Python nao encontrado — instalar automaticamente
echo [!] Python nao encontrado no sistema.
echo     Baixando e instalando automaticamente...
echo     Isso pode levar alguns minutos, aguarde...
echo.

set "PY_INSTALLER=%~dp0python_installer.exe"
set "PY_URL=https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"

echo     Baixando Python 3.12...
powershell -Command "Invoke-WebRequest -Uri '!PY_URL!' -OutFile '!PY_INSTALLER!'" 2>nul
if not exist "!PY_INSTALLER!" (
    echo.
    echo [ERRO] Falha ao baixar o Python.
    echo        Verifique sua conexao com a internet e tente novamente.
    echo        Ou instale manualmente: https://www.python.org/downloads/
    echo        IMPORTANTE: Marque "Add Python to PATH" durante a instalacao.
    echo.
    if "!AUTOSTART!"=="0" pause
    exit /b 1
)

:: Desinstalar Python anterior (limpa registros MSI que impedem reinstalacao limpa)
"!PY_INSTALLER!" /uninstall /quiet >nul 2>&1

:: Limpar pasta residual se existir
if exist "%LOCALAPPDATA%\Programs\Python\Python312" (
    rmdir /s /q "%LOCALAPPDATA%\Programs\Python\Python312" >nul 2>&1
)

echo     Instalando Python 3.12 (isso pode demorar um pouco)...
"!PY_INSTALLER!" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1
set "INSTALL_EXIT=!ERRORLEVEL!"
del "!PY_INSTALLER!" >nul 2>&1

if not "!INSTALL_EXIT!"=="0" (
    echo.
    echo [ERRO] Falha ao instalar o Python.
    echo        Tente instalar manualmente: https://www.python.org/downloads/
    echo        IMPORTANTE: Marque "Add Python to PATH" durante a instalacao.
    echo.
    if "!AUTOSTART!"=="0" pause
    exit /b 1
)

:: Usar caminho completo (PATH pode nao estar atualizado nesta sessao)
set "PYTHON_CMD=!PY_FULL_PATH!"

:: Verificar se instalou corretamente
"!PYTHON_CMD!" -c "import encodings" >nul 2>&1
if not !ERRORLEVEL!==0 (
    echo.
    echo [ERRO] Python foi instalado mas nao esta funcionando corretamente.
    echo        Tente reiniciar o computador e executar windows_start.bat novamente.
    echo        Ou instale manualmente: https://www.python.org/downloads/
    echo.
    if "!AUTOSTART!"=="0" pause
    exit /b 1
)

echo     Python instalado com sucesso!
echo.

:: ===== 2. VERIFICAR VERSAO DO PYTHON >= 3.11 =====
:check_version
set "PY_VER="
for /f "tokens=2 delims= " %%v in ('"!PYTHON_CMD!" --version 2^>^&1') do set "PY_VER=%%v"

set "PY_MAJOR="
set "PY_MINOR="
for /f "tokens=1,2 delims=." %%a in ("!PY_VER!") do (
    set "PY_MAJOR=%%a"
    set "PY_MINOR=%%b"
)

set "VERSION_OK=0"
if !PY_MAJOR! GEQ 4 set "VERSION_OK=1"
if !PY_MAJOR!==3 if !PY_MINOR! GEQ 11 set "VERSION_OK=1"

if !VERSION_OK!==0 (
    echo [ERRO] Python !PY_VER! detectado, mas o WhatsBot-Lite precisa do 3.11 ou superior.
    echo        Baixe a versao mais recente em: https://www.python.org/downloads/
    echo.
    if "!AUTOSTART!"=="0" pause
    exit /b 1
)

echo [OK] Python !PY_VER! encontrado.

:: ===== 3. VERIFICAR PIP =====
"!PYTHON_CMD!" -m pip --version >nul 2>&1
if not !ERRORLEVEL!==0 (
    echo [!] pip nao encontrado. Tentando recuperar...
    "!PYTHON_CMD!" -m ensurepip >nul 2>&1
    "!PYTHON_CMD!" -m pip --version >nul 2>&1
    if not !ERRORLEVEL!==0 (
        echo [!] ensurepip falhou. Baixando get-pip.py...
        powershell -Command "Invoke-WebRequest -Uri 'https://bootstrap.pypa.io/get-pip.py' -OutFile '%~dp0get-pip.py'" 2>nul
        if exist "%~dp0get-pip.py" (
            "!PYTHON_CMD!" "%~dp0get-pip.py" >nul 2>&1
            del "%~dp0get-pip.py" >nul 2>&1
        )
        "!PYTHON_CMD!" -m pip --version >nul 2>&1
        if not !ERRORLEVEL!==0 (
            echo.
            echo [ERRO] pip nao disponivel.
            echo        Reinstale o Python marcando a opcao "pip" no instalador.
            echo        https://www.python.org/downloads/
            echo.
            if "!AUTOSTART!"=="0" pause
            exit /b 1
        )
    )
)
echo [OK] pip disponivel.

:: ===== 4. VERIFICAR GOWA =====
if not exist "bin\gowa.exe" (
    echo.
    echo [ERRO] bin\gowa.exe nao encontrado!
    echo        O download do WhatsBot-Lite esta incompleto ou corrompido.
    echo        Baixe novamente o WhatsBot-Lite completo.
    echo.
    if "!AUTOSTART!"=="0" pause
    exit /b 1
)
echo [OK] gowa.exe encontrado.

:: ===== 5. VERIFICAR PORTAS =====
:: (processos anteriores ja foram encerrados no inicio do script)

:: ===== 6. CONFIGURAR AMBIENTE =====
if not exist "venv" (
    echo.
    echo Primeira execucao detectada. Configurando ambiente...
    echo Isso pode levar alguns minutos, aguarde...
    echo.
    "!PYTHON_CMD!" -m venv venv
)
call venv\Scripts\activate.bat
echo Verificando dependencias...
pip install -q -r requirements.txt

echo.
echo [OK] Ambiente pronto!

:: Quando iniciado pelo atalho oculto, rodar o servidor neste processo. Assim o
:: WhatsBot-Lite volta no proximo logon sem abrir uma aba do navegador.
if "!AUTOSTART!"=="1" (
    endlocal
    goto :server
)

endlocal

:: Abrir browser apos 5s
start "" cmd /c "timeout /t 5 /nobreak >nul & start http://127.0.0.1:8080"

:: Relancar este script no modo servidor (janela oculta) e fechar este terminal
powershell -Command "Start-Process cmd -ArgumentList '/c title WhatsBot-Lite-Server && cd /d %~dp0 && call windows_start.bat --server' -WindowStyle Hidden"
exit

:: ===== MODO SERVIDOR (janela oculta, apenas roda o uvicorn) =====
:server
cd /d "%~dp0"
call venv\Scripts\activate.bat
set NO_COLOR=1
:: Ha supervisor (o --reload religa o servidor depois do reinicio): libera o reinicio automatico do Importar/Exportar.
set WHATSBOT_SUPERVISED=1
:: uvicorn valida cada --reload-dir antes de subir; storages\plugins e criada
:: em runtime, entao precisa existir antes (senao o uvicorn aborta).
if not exist "storages\plugins" mkdir "storages\plugins"
uvicorn server.dev:app --host 0.0.0.0 --port 8080 --reload --reload-dir server --reload-dir agent --reload-dir config --reload-dir gowa --reload-dir db --reload-dir plugins --reload-dir storages\plugins --log-level warning
exit /b

:: ===== CONFIGURACAO DA INICIALIZACAO AUTOMATICA =====
:ask_autostart
echo.
echo ========================================
echo   Iniciar WhatsBot-Lite automaticamente?
echo ========================================
echo.
echo [1] Sim - iniciar ao entrar no Windows
echo [2] Nao - iniciar somente quando eu abrir esta BAT
echo.
choice /C 12 /N /M "Escolha uma opcao"

if errorlevel 2 (
    if not exist "storages" mkdir "storages"
    > "storages\windows_autostart_choice.txt" echo disabled
    echo.
    echo [OK] O WhatsBot-Lite sera iniciado somente quando esta BAT for aberta.
    exit /b 0
)

call :install_autostart
if errorlevel 1 (
    echo.
    echo [AVISO] Nao foi possivel ativar a inicializacao automatica.
    echo          A pergunta sera exibida novamente na proxima abertura.
    exit /b 0
)

if not exist "storages" mkdir "storages"
> "storages\windows_autostart_choice.txt" echo enabled
echo.
echo [OK] Inicializacao automatica ativada.
echo      Apos desligar e ligar, o WhatsBot-Lite inicia ao entrar no Windows.
exit /b 0

:install_autostart
set "WHATSBOT_DIR=%~dp0"

:: Um atalho na pasta Inicializar do usuario nao exige permissao de administrador
:: (ao contrario de algumas configuracoes do Agendador de Tarefas).
:: Gerar as aspas com [char]34 evita que sejam consumidas entre CMD e PowerShell.
:: O comando fica no proprio atalho, sem precisar de um arquivo PS1 separado.
:: Resolver a BAT pelo diretorio do atalho evita inserir o caminho como codigo.
:: O PowerShell e o CMD iniciam ocultos; WindowStyle = 7 sozinho so minimiza.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference = 'Stop'; " ^
  "$command = '$dir = (Get-Location).Path; $bat = Join-Path $dir ''windows_start.bat''; ' + " ^
  "  'Start-Process -FilePath $env:ComSpec -ArgumentList (''/d /s /c {0}{0}{1}{0} --autostart{0}'' -f [char]34, $bat) -WorkingDirectory $dir -WindowStyle Hidden'; " ^
  "$startup = [Environment]::GetFolderPath('Startup'); " ^
  "$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $startup 'WhatsBot.lnk')); " ^
  "$shortcut.TargetPath = Join-Path $PSHOME 'powershell.exe'; " ^
  "$shortcut.Arguments = ('-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -Command {0}{1}{0}' -f [char]34, $command); " ^
  "$shortcut.WorkingDirectory = $env:WHATSBOT_DIR; " ^
  "$shortcut.WindowStyle = 7; " ^
  "$shortcut.Description = 'Inicia o WhatsBot-Lite em segundo plano no logon.'; " ^
  "$shortcut.Save()"

if errorlevel 1 exit /b 1
exit /b 0
