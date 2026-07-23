import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
def load():
 spec=importlib.util.spec_from_file_location("pm_update",ROOT/"scripts"/"portable_manager.py"); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

class UpdatePipelineTests(unittest.TestCase):
 def setUp(self): self.m=load()
 def test_parse_ls_remote(self):
  sha="a"*40
  self.assertEqual(sha,self.m.parse_ls_remote(f"{sha}\trefs/heads/main\n","main"))
  with self.assertRaises(ValueError): self.m.parse_ls_remote("garbage","main")
 def test_copy_protected_paths_preserves_config_and_data(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t); src=root/'old'; dst=root/'new'; src.mkdir(); dst.mkdir()
   (src/'global_config.yml').write_text('secret: yes',encoding='utf-8')
   (src/'data').mkdir(); (src/'data'/'x.db').write_bytes(b'data')
   self.m.copy_runtime_data(src,dst,['global_config.yml','data'])
   self.assertEqual('secret: yes',(dst/'global_config.yml').read_text(encoding='utf-8'))
   self.assertEqual(b'data',(dst/'data'/'x.db').read_bytes())
 def test_backup_manifest_contains_sha256(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t); app=root/'app'; app.mkdir(); (app/'global_config.yml').write_text('x',encoding='utf-8')
   backup=self.m.backup_runtime_data(root,app,['global_config.yml'])
   manifest=(backup/'manifest.json').read_text(encoding='utf-8')
   self.assertIn('sha256',manifest)
 def test_retry_call_recovers_from_transient_failure(self):
  calls=[]
  def action():
   calls.append(1)
   if len(calls)<3: raise RuntimeError("reset")
   return "ok"
  self.assertEqual("ok",self.m.retry_call(action,attempts=3,delay=0))
  self.assertEqual(3,len(calls))


if __name__=='__main__': unittest.main()
