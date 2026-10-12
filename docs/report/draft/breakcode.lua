-- Let inline code break at _ / . - = so long test names and paths wrap in tables.
local function esc(s)
  s = s:gsub("\\", "\\textbackslash{}")
  s = s:gsub("([#$%%&{}])", "\\%1")
  s = s:gsub("~", "\\textasciitilde{}")
  s = s:gsub("%^", "\\textasciicircum{}")
  return s
end
function Code(el)
  if FORMAT ~= "latex" then return nil end
  local s = esc(el.text)
  s = s:gsub("_", "\\_\\allowbreak{}")
  s = s:gsub("/", "/\\allowbreak{}")
  s = s:gsub("%.", ".\\allowbreak{}")
  s = s:gsub("%-", "-\\allowbreak{}")
  s = s:gsub("=", "=\\allowbreak{}")
  return pandoc.RawInline("latex", "\\texttt{" .. s .. "}")
end
