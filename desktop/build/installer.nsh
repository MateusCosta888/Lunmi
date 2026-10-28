!ifndef BUILD_UNINSTALLER
  !macro customInit
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
!endif

; Remoção Spotify depois de o builder fechar a Lunmi e antes de apagar o runtime.
; O script Python chama o MESMO job consentido de `SpotifyInstaller`; o NSIS não
; replica remoção de ficheiros. Código 2 = job falhou/adiou: parar e conservar
; estado/backup, com instruções.
!macro customUnInstall
  ${IfNot} ${isUpdated}
    IfFileExists "$INSTDIR\resources\app\app\integrations\spotify_uninstall.py" 0 lunmi_spotify_uninstall_done
    IfFileExists "$INSTDIR\resources\python\python.exe" 0 lunmi_spotify_uninstall_done
    IfFileExists "$LOCALAPPDATA\Lunmi\.local\spotify\state.json" 0 lunmi_spotify_uninstall_done
    MessageBox MB_YESNO|MB_DEFBUTTON2|MB_ICONQUESTION "Remover a ligação Spotify da Lunmi antes de desinstalar? O Spotify pode fechar e voltar a abrir; a conta e os ficheiros pessoais ficam.\n\nSe recusares, a Lunmi não é desinstalada e o estado/backup ficam guardados." IDYES lunmi_spotify_uninstall_run
    Abort
    lunmi_spotify_uninstall_run:
    nsExec::ExecToStack '"$INSTDIR\resources\python\python.exe" "$INSTDIR\resources\app\app\integrations\spotify_uninstall.py" --state-root "$LOCALAPPDATA\Lunmi" --approved'
    Pop $1
    Pop $2
    ${If} $1 != 0
      MessageBox MB_ICONSTOP|MB_OK "A remoção da ligação Spotify não terminou (motivo: $2). A Lunmi não foi desinstalada. O estado e o backup ficam em $LOCALAPPDATA\Lunmi\.local\spotify; abre a Lunmi, resolve a ligação e tenta novamente."
      Abort
    ${EndIf}
  ${EndIf}
  lunmi_spotify_uninstall_done:
!macroend
