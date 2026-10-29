; Componentes pesados (Python e voz): chaves, arquivos e tamanhos gerados por
; desktop/scripts/components.py. Ficam em "<instalação>-runtime\<nome>-<chave>", fora de
; $INSTDIR, e só voltam a ser extraídos quando o conteúdo muda.
!include "${BUILD_RESOURCES_DIR}\components\lunmi-components.nsh"

!ifndef BUILD_UNINSTALLER
  Var lunmiUpdateDir
  !macro customInit
    ; The app starts updates with /S. A multi-minute install without a window looks like Lunmi vanished.
    ${If} ${isUpdated}
      StrCpy $lunmiUpdateDir $INSTDIR
      SetSilent normal
    ${EndIf}
    ; Manual setups start in the standard per-user folder; updates keep /D and the installed path.
    ${IfNot} ${isUpdated}
      !insertmacro GetDParameter $R0
      ${If} $R0 == ""
        StrCpy $INSTDIR "$LOCALAPPDATA\Programs\${APP_FILENAME}"
      ${EndIf}
    ${EndIf}
  !macroend

  !macro customInstallMode
    ; The application only supports per-user installation; do not reload an old test destination.
    ${If} $installMode == "CurrentUser"
      Abort
    ${EndIf}
  !macroend

  ; Nothing has been removed yet: reopen the installed version (updates) and stop with code 2.
  !macro lunmiAbort
    ${If} ${isUpdated}
    ${AndIf} ${FileExists} "$appExe"
      ${StdUtils.ExecShellAsUser} $0 "$appExe" "open" ""
    ${EndIf}
    SetErrorLevel 2
    Quit
  !macroend

  ; Free MB needed on ROOT, or abort before anything is removed.
  !macro lunmiNeed ROOT MB
    ClearErrors
    ${DriveSpace} "${ROOT}\" "/D=F /S=M" $lunmiOut
    ${IfNot} ${Errors}
    ${AndIf} $lunmiOut < ${MB}
      MessageBox MB_OK|MB_ICONEXCLAMATION "Espaço insuficiente em ${ROOT}: a instalação precisa de ${MB} MB livres e há $lunmiOut MB. Nada foi alterado na versão instalada; liberta espaço e executa novamente o instalador." /SD IDOK
      !insertmacro lunmiAbort
    ${EndIf}
  !macroend

  ; Peak on the destination volume: the trees still missing, the archive being extracted next to
  ; them (one at a time, the larger counts) and the application files. Temp holds the app package.
  ; shortcut: LUNMI_APP_MB is a fixed margin, not the measured app size; measure it if the app grows past it.
  !define LUNMI_APP_MB 1024
  !macro lunmiCheckSpace
    StrCpy $lunmiValue ${LUNMI_APP_MB}
    StrCpy $lunmiHandle 0
    ${IfNot} ${FileExists} "$lunmiRuntime\python-${LUNMI_PYTHON_KEY}\.lunmi-complete"
      IntOp $lunmiValue $lunmiValue + ${LUNMI_PYTHON_MB}
      StrCpy $lunmiHandle ${LUNMI_PYTHON_ARCHIVE_MB}
    ${EndIf}
    ${IfNot} ${FileExists} "$lunmiRuntime\voice-${LUNMI_VOICE_KEY}\.lunmi-complete"
      IntOp $lunmiValue $lunmiValue + ${LUNMI_VOICE_MB}
      ${If} ${LUNMI_VOICE_ARCHIVE_MB} > $lunmiHandle
        StrCpy $lunmiHandle ${LUNMI_VOICE_ARCHIVE_MB}
      ${EndIf}
    ${EndIf}
    IntOp $lunmiValue $lunmiValue + $lunmiHandle
    IntOp $lunmiValue $lunmiValue + 512
    ${GetRoot} "$INSTDIR" $lunmiTarget
    ${GetRoot} "$PLUGINSDIR" $lunmiHandle
    ${If} $lunmiHandle == $lunmiTarget
      IntOp $lunmiValue $lunmiValue + ${LUNMI_APP_MB}
    ${Else}
      !insertmacro lunmiNeed $lunmiHandle ${LUNMI_APP_MB}
    ${EndIf}
    !insertmacro lunmiNeed $lunmiTarget $lunmiValue
  !macroend

  ; Extracts into "<name>-<key>.partial" and renames only when complete; the marker is written last.
  !macro lunmiStage NAME KEY ARCHIVE SENTINEL
    StrCpy $lunmiTarget "$lunmiRuntime\${NAME}-${KEY}"
    ${IfNot} ${FileExists} "$lunmiTarget\.lunmi-complete"
      RMDir /r "$lunmiTarget.partial"
      RMDir /r "$lunmiTarget"
      CreateDirectory "$lunmiTarget.partial"
      !ifdef COMPRESS
        SetCompress off
      !endif
      File "/oname=$lunmiRuntime\${NAME}.7z" "${ARCHIVE}"
      !ifdef COMPRESS
        SetCompress "${COMPRESS}"
      !endif
      DetailPrint "A preparar ${NAME}: pode demorar alguns minutos..."
      ; 7za checks every CRC and exits non-zero on any failure; Nsis7z::Extract would report success.
      nsExec::ExecToStack '"$PLUGINSDIR\7za.exe" x -y -bd -bso0 -bsp0 "-o$lunmiTarget.partial" "$lunmiRuntime\${NAME}.7z"'
      Pop $lunmiOut
      Pop $lunmiValue
      Delete "$lunmiRuntime\${NAME}.7z"
      StrCpy $lunmiValue "failed"
      ${If} $lunmiOut == 0
      ${AndIf} ${FileExists} "$lunmiTarget.partial\${SENTINEL}"
        ClearErrors
        Rename "$lunmiTarget.partial" "$lunmiTarget"
        ${IfNot} ${Errors}
          FileOpen $lunmiHandle "$lunmiTarget\.lunmi-complete" w
          ${IfNot} ${Errors}
            FileWrite $lunmiHandle "${KEY}"
            FileClose $lunmiHandle
            StrCpy $lunmiValue "ok"
          ${EndIf}
        ${EndIf}
      ${EndIf}
      ${If} $lunmiValue != "ok"
        RMDir /r "$lunmiTarget.partial"
        MessageBox MB_OK|MB_ICONEXCLAMATION "Não foi possível preparar os componentes da Lunmi (${NAME}). Nada foi alterado na versão instalada. Confirma o espaço em disco e o antivírus e executa novamente o instalador." /SD IDOK
        !insertmacro lunmiAbort
      ${EndIf}
    ${EndIf}
  !macroend

  ; Defining customCheckAppRunning makes the builder skip these; its _CHECK_APP_RUNNING needs them.
  !include "getProcessInfo.nsh"
  Var pid

  ; Runs where the builder closes the app, before it removes the previous version.
  !macro customCheckAppRunning
    ; A visible update runs the builder's instFilesPre, which appends \${APP_FILENAME} to folders
    ; without the app name; the old uninstaller would then delete the components staged inside it.
    ${If} ${isUpdated}
      StrCpy $INSTDIR $lunmiUpdateDir
      ; installSection derived these before this point; shortcuts, reopen and recovery use them.
      StrCpy $appExe "$INSTDIR\${APP_EXECUTABLE_FILENAME}"
    ${EndIf}
    !insertmacro IS_POWERSHELL_AVAILABLE
    !insertmacro _CHECK_APP_RUNNING
    Var /GLOBAL lunmiRuntime
    Var /GLOBAL lunmiTarget
    Var /GLOBAL lunmiValue
    Var /GLOBAL lunmiOut
    Var /GLOBAL lunmiHandle
    StrCpy $lunmiRuntime "$INSTDIR-runtime"
    CreateDirectory $lunmiRuntime
    File "/oname=$PLUGINSDIR\7za.exe" "${LUNMI_SEVEN_ZIP}"
    !insertmacro lunmiCheckSpace
    !insertmacro lunmiStage python ${LUNMI_PYTHON_KEY} "${LUNMI_PYTHON_ARCHIVE}" "python.exe"
    !insertmacro lunmiStage voice ${LUNMI_VOICE_KEY} "${LUNMI_VOICE_ARCHIVE}" "runtime\python.exe"
  !macroend

  !macro customInstall
    ; Drop component versions this installation no longer uses (old keys, interrupted extractions).
    StrCpy $lunmiRuntime "$INSTDIR-runtime"
    FindFirst $lunmiHandle $lunmiValue "$lunmiRuntime\*"
    ${DoWhile} $lunmiValue != ""
      ${If} $lunmiValue != "."
      ${AndIf} $lunmiValue != ".."
      ${AndIf} $lunmiValue != "python-${LUNMI_PYTHON_KEY}"
      ${AndIf} $lunmiValue != "voice-${LUNMI_VOICE_KEY}"
        RMDir /r "$lunmiRuntime\$lunmiValue"
      ${EndIf}
      FindNext $lunmiHandle $lunmiValue
    ${Loop}
    FindClose $lunmiHandle
    ClearErrors
    ; Update from the app: reopen Lunmi and close the progress window, like the former silent run.
    ${If} ${isUpdated}
    ${AndIf} ${isForceRun}
      HideWindow
      ${StdUtils.ExecShellAsUser} $0 "$launchLink" "open" "--updated"
      SetErrorLevel 0
      Quit
    ${EndIf}
  !macroend

  ; Replaces the builder check: same outcome, but explains it and reopens the version that remains.
  !macro customUnInstallCheck
    ${If} ${Errors}
      MessageBox MB_OK|MB_ICONEXCLAMATION "Não foi possível iniciar a remoção da versão anterior da Lunmi. A instalação não foi concluída. Os teus dados em $LOCALAPPDATA\Lunmi foram mantidos.$\n$\nExecuta novamente o instalador; se o erro continuar, desinstala a Lunmi nas Definições do Windows e instala de novo." /SD IDOK
      !insertmacro lunmiAbort
    ${ElseIf} $R0 != 0
      MessageBox MB_OK|MB_ICONEXCLAMATION "Não foi possível substituir a versão anterior da Lunmi (código $R0). A instalação não foi concluída. Os teus dados em $LOCALAPPDATA\Lunmi foram mantidos.$\n$\nFecha a Lunmi e os programas que possam estar a usar os seus ficheiros e executa novamente o instalador."
      !insertmacro lunmiAbort
    ${EndIf}
  !macroend
!endif

; Remoção Spotify depois de o builder fechar a Lunmi e antes de apagar o runtime.
; O script Python chama o MESMO job consentido de `SpotifyInstaller`; o NSIS não
; replica remoção de ficheiros. Código 2 = job falhou/adiou: parar e conservar
; estado/backup, com instruções.
!macro customUnInstall
  ; QA builds define LUNMI_QA_NO_SPOTIFY: their uninstall must never reach the real Spotify state.
  !ifndef LUNMI_QA_NO_SPOTIFY
  ${IfNot} ${isUpdated}
    IfFileExists "$INSTDIR\resources\app\app\integrations\spotify_uninstall.py" 0 lunmi_spotify_uninstall_done
    IfFileExists "$INSTDIR-runtime\python-${LUNMI_PYTHON_KEY}\python.exe" 0 lunmi_spotify_uninstall_done
    IfFileExists "$LOCALAPPDATA\Lunmi\.local\spotify\state.json" 0 lunmi_spotify_uninstall_done
    MessageBox MB_YESNO|MB_DEFBUTTON2|MB_ICONQUESTION "Remover a ligação Spotify da Lunmi antes de desinstalar? O Spotify pode fechar e voltar a abrir; a conta e os ficheiros pessoais ficam.\n\nSe recusares, a Lunmi não é desinstalada e o estado/backup ficam guardados." IDYES lunmi_spotify_uninstall_run
    Abort
    lunmi_spotify_uninstall_run:
    nsExec::ExecToStack '"$INSTDIR-runtime\python-${LUNMI_PYTHON_KEY}\python.exe" "$INSTDIR\resources\app\app\integrations\spotify_uninstall.py" --state-root "$LOCALAPPDATA\Lunmi" --approved'
    Pop $1
    Pop $2
    ${If} $1 != 0
      MessageBox MB_ICONSTOP|MB_OK "A remoção da ligação Spotify não terminou (motivo: $2). A Lunmi não foi desinstalada. O estado e o backup ficam em $LOCALAPPDATA\Lunmi\.local\spotify; abre a Lunmi, resolve a ligação e tenta novamente."
      Abort
    ${EndIf}
  ${EndIf}
  lunmi_spotify_uninstall_done:
  !endif
  ; Uma desinstalação real leva também os componentes; uma atualização conserva-os.
  ${IfNot} ${isUpdated}
    RMDir /r "$INSTDIR-runtime"
  ${EndIf}
!macroend
