import shutil, os
src = r"C:\Users\wesix\OneDrive\Desktop\Media Hub\apps\panel-premiere"
dst = os.path.join(os.environ["APPDATA"], r"Adobe\UXP\Plugins\External\com.sixgricks.framefound")
if os.path.exists(dst):
    shutil.rmtree(dst)
os.makedirs(dst)
for f in ["manifest.json", "index.html", "main.js"]:
    shutil.copy(os.path.join(src, f), dst)
    print("copied", f)
icons_src = os.path.join(src, "icons")
if os.path.isdir(icons_src):
    shutil.copytree(icons_src, os.path.join(dst, "icons"))
    print("copied icons/")
print("Done.")
input("Press Enter to close")
