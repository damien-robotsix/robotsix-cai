import sys
s = "like \`grep\`"
if "\\" in s:
    print("BACKSLASH IS PRESENT")
else:
    print("BACKSLASH IS MISSING")

s2 = "\`grep\`"
print(s2)
