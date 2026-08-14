; Keep electron-builder's maintained NSIS installer flow, but make its two
; progress passes understandable. The first pass stages the embedded archive;
; the second extracts/copies it into the selected installation directory.
;
; customPageAfterChangeDir runs immediately before MUI_PAGE_INSTFILES in the
; assisted installer, so this callback applies to the progress page only.
!macro customPageAfterChangeDir
  !define MUI_PAGE_CUSTOMFUNCTION_SHOW CapoInstFilesShow
!macroend

; electron-builder calls the architecture-specific customFiles hook directly
; after the application archive has been extracted and copied. The remaining
; work is registration, the uninstaller, and shortcuts.
!macro customFiles_x64
  FindWindow $0 "#32770" "" $HWNDPARENT
  GetDlgItem $0 $0 1006
  SendMessage $0 ${WM_SETTEXT} 0 "STR:Finalizing ${PRODUCT_NAME} and creating shortcuts..."
  DetailPrint "Finalizing ${PRODUCT_NAME}..."
!macroend

; customHeader is expanded after MUI2 has been loaded, which makes the header
; and progress-page controls available without replacing the stock installer.
!macro customHeader
  ; electron-builder includes this same file while compiling the generated
  ; uninstaller. That build has no assisted installer progress page, so an
  ; unguarded callback is unused and NSIS warning 6010 becomes a hard error.
  !ifndef BUILD_UNINSTALLER
    Function CapoInstFilesShow
      !insertmacro MUI_HEADER_TEXT \
        "Installing ${PRODUCT_NAME}" \
        "Extracting the bundled application, then installing it to the selected folder."

      FindWindow $0 "#32770" "" $HWNDPARENT
      GetDlgItem $0 $0 1006
      SendMessage $0 ${WM_SETTEXT} 0 "STR:Extracting and installing ${PRODUCT_NAME}..."
    FunctionEnd
  !endif
!macroend
