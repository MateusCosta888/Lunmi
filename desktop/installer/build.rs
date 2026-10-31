fn main() {
    let mut res = winresource::WindowsResource::new();
    res.set_icon("../src/assets/branding/lunmi.ico")
        .set("FileDescription", "Instalador da Lunmi")
        .set("ProductName", "Lunmi")
        .set_manifest(r#"<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
<trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges>
<requestedExecutionLevel level="asInvoker" uiAccess="false"/></requestedPrivileges></security></trustInfo>
<application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>
<dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2</dpiAwareness>
</windowsSettings></application>
<dependency><dependentAssembly><assemblyIdentity type="win32" name="Microsoft.Windows.Common-Controls" version="6.0.0.0" processorArchitecture="*" publicKeyToken="6595b64144ccf1df" language="*"/></dependentAssembly></dependency>
</assembly>"#);
    res.compile().expect("recursos do Windows");
    println!("cargo:rerun-if-changed=ui");
    println!("cargo:rerun-if-changed=../src/features/chat/orb/liveRenderer.js");
}
