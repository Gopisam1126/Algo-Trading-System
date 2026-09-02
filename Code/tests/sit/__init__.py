"""System integration testing.

QA asks "is this component correct". SIT asks a different question: **does the
assembled thing do the right thing across a realistic session, when something
goes wrong?**

These tests are allowed to find what component tests cannot, and when they do,
that is evidence the seam only exists in the assembled system — not a QA
failure. Defects found here are logged in the tracker's ``SIT Defects`` sheet
with the column that matters most: *why component tests missed it*.
"""
